"""应用配置：全部通过环境变量 / .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "AI 作业评审平台"
    api_prefix: str = "/api"
    debug: bool = True

    # 数据库
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'review.db'}"

    # 对象存储
    storage_backend: str = "minio"  # minio | local
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_bucket: str = "review-platform"
    minio_secure: bool = False
    minio_public_endpoint: str = ""
    local_storage_dir: str = str(BACKEND_DIR / "data" / "objects")

    # AI 评审
    ai_reviewer: str = "mock"  # mock | openai
    ai_review_workers: int = 2

    # 上传
    max_upload_mb: int = 300

    # CORS，逗号分隔
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
