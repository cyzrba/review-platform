"""文件存放：全部落本地磁盘。

所有文件读写都走这里，业务代码不直接碰文件路径 —— 以后真要换成对象存储
（MinIO / S3），只要再实现一个 ObjectStorage 子类即可。

``key`` 是相对路径，例如 ``istudy/works/25人工智能本1-第1周作业/students/...``，
数据库里存的就是它，所以整个 data 目录换盘符搬走也不会失效。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
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

    def move_prefix(self, old_prefix: str, new_prefix: str) -> int:
        """把 old_prefix 下的对象整体挪到 new_prefix 下，返回挪动的对象数。

        用来给「目录」改名，比如把 istudy/works/9 换成 istudy/works/25人工智能本1-第1周作业。
        后端不支持时返回 0。
        """
        return 0

    def delete_prefix(self, prefix: str) -> int:
        """删掉 prefix 下面的所有对象，返回删除个数。"""
        return 0

    def presigned_url(self, key: str, expires_seconds: int = 3600) -> str | None:
        """返回可直链下载的地址；不支持时返回 None。"""
        return None

    def health(self) -> tuple[bool, str | None]:
        return True, None


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

    def move_prefix(self, old_prefix: str, new_prefix: str) -> int:
        source = self._path(old_prefix)
        if not source.exists():
            return 0
        target = self._path(new_prefix)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            source.rename(target)
        except OSError:
            # Windows 上目录改名经常被别的进程占着（文件资源管理器、杀软、
            # 文件监听都算），这时退化成「逐个复制 + 删原目录」。
            import shutil

            target.mkdir(parents=True, exist_ok=True)
            moved = 0
            for item in sorted(source.rglob("*")):
                if not item.is_file():
                    continue
                destination = target / item.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                moved += 1
            shutil.rmtree(source, ignore_errors=True)
            return moved

        return sum(1 for item in target.rglob("*") if item.is_file())

    def delete_prefix(self, prefix: str) -> int:
        import shutil

        path = self._path(prefix)
        if not path.exists():
            return 0
        if path.is_file():
            path.unlink()
            return 1
        removed = sum(1 for item in path.rglob("*") if item.is_file())
        shutil.rmtree(path, ignore_errors=True)
        return removed


_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    """进程内单例。"""
    global _storage
    if _storage is None:
        _storage = LocalStorage()
        logger.info("对象存储后端: %s", _storage.name)
    return _storage


def reset_storage() -> None:
    """测试用：切配置后重建。"""
    global _storage
    _storage = None
