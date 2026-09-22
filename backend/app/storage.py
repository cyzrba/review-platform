"""对象存储抽象：MinIO 为主，本地磁盘为兜底。

所有文件读写都走这里，业务代码不直接依赖 minio SDK，
这样本地没有 MinIO 时把 STORAGE_BACKEND 改成 local 就能跑通全流程。
"""

from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)


class ObjectStorage(ABC):
    name: str

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """写入对象，返回 object key。"""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """读取对象内容。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象（不存在时静默忽略）。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    def presigned_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        """返回可直链下载的地址；不支持时返回 None。"""
        return None

    def health(self) -> tuple[bool, str | None]:
        return True, None


class MinioStorage(ObjectStorage):
    name = "minio"

    def __init__(self) -> None:
        from minio import Minio  # 延迟导入，local 模式下不需要 minio

        self.bucket = settings.minio_bucket
        self.endpoint = settings.minio_endpoint
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info("已创建 MinIO bucket: %s", self.bucket)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MinIO bucket 检查失败（后续上传会重试）: %s", exc)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        response = None
        try:
            response = self.client.get_object(self.bucket, key)
            return response.read()
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def delete(self, key: str) -> None:
        from minio.error import S3Error

        try:
            self.client.remove_object(self.bucket, key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchBucket"}:
                raise

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error:
            return False

    def presigned_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        try:
            url = self.client.presigned_get_object(
                self.bucket, key, expires=timedelta(seconds=expires_seconds)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("生成 MinIO 直链失败: %s", exc)
            return None
        if settings.minio_public_endpoint:
            # 容器内 endpoint 与浏览器可达地址不一致时做一次替换
            url = url.replace(
                f"{'https' if settings.minio_secure else 'http'}://{settings.minio_endpoint}",
                settings.minio_public_endpoint.rstrip("/"),
                1,
            )
        return url

    def health(self) -> tuple[bool, str | None]:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            return True, f"bucket={self.bucket}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


class LocalStorage(ObjectStorage):
    """把对象落到本地目录，key 直接映射成路径。"""

    name = "local"

    def __init__(self) -> None:
        self.root = Path(settings.local_storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.bucket = f"local:{self.root}"
        self.endpoint = None

    def _path(self, key: str) -> Path:
        safe = Path(key.replace("\\", "/"))
        if safe.is_absolute() or ".." in safe.parts:
            raise ValueError(f"非法 object key: {key}")
        return self.root / safe

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    """进程内单例。"""
    global _storage
    if _storage is None:
        if settings.storage_backend.lower() == "minio":
            _storage = MinioStorage()
        else:
            _storage = LocalStorage()
        logger.info("对象存储后端: %s", _storage.name)
    return _storage


def reset_storage() -> None:
    """测试用：切配置后重建。"""
    global _storage
    _storage = None
