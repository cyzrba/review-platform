"""清空 MinIO bucket 里的对象，便于重新做过演示或联调。

用法：
    MINIO_ENDPOINT=localhost:9000 MINIO_ACCESS_KEY=minioadmin \
    MINIO_SECRET_KEY=minioadmin123 MINIO_BUCKET=review-platform \
    ./.venv/bin/python scripts/clear_storage.py

默认只清空对象、保留 bucket；加 --drop-bucket 连 bucket 一起删掉。
"""

from __future__ import annotations

import sys

from app.config import settings
from app.storage import get_storage


def main() -> int:
    if settings.storage_backend.lower() != "minio":
        print(f"当前存储后端是 {settings.storage_backend}，本脚本只处理 MinIO")
        return 0

    from minio import Minio

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    bucket = settings.minio_bucket
    if not client.bucket_exists(bucket):
        print(f"bucket {bucket} 不存在，无需清理")
        return 0

    storage = get_storage()
    objects = list(client.list_objects(bucket, recursive=True))
    for item in objects:
        storage.delete(item.object_name)
    print(f"已从 {bucket} 删除 {len(objects)} 个对象")

    if "--drop-bucket" in sys.argv:
        client.remove_bucket(bucket)
        print(f"已删除 bucket {bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
