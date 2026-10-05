"""应用配置：全部通过环境变量 / .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"


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
    database_url: str = f"sqlite:///{(DATA_DIR / 'review.db').as_posix()}"
    # SQLite 日志模式。DELETE = 每笔提交直接写主库文件（单机工具推荐，用数据库
    # 工具随时打开都是最新的）；WAL = 先写 -wal 文件、性能好但主库会滞后。
    sqlite_journal_mode: str = "DELETE"

    # 文件存放：默认落在 backend/data/objects，跟着项目目录走，换盘符不用改配置
    local_storage_dir: str = str(DATA_DIR / "objects")

    # AI 评审
    ai_reviewer: str = "mock"  # mock | llm
    ai_review_workers: int = 2

    # 大模型评审（OpenAI 兼容接口，DeepSeek 也是这一套）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-v4.1-flash"
    llm_timeout: float = 180.0
    llm_temperature: float = 0.0
    # 带「思考」的模型会把推理也算进 completion，给少了就会出现
    # finish_reason=length 且 content 为空，所以默认给足
    llm_max_tokens: int = 8000
    # 有些网关支持这么关掉「思考」：实测 deepseek-flash 开了之后
    # 思考 token 从几千直接变 0，又快又省；不支持的话网关会忽略这个字段
    llm_disable_thinking: bool = True
    llm_retries: int = 2
    # 送模型前把图片压一下：手机拍的作答照片一张就 1.7MB，压到长边 1600 就够看清字了
    llm_image_max_edge: int = 1600
    llm_image_quality: int = 85
    # 一次最多给模型看几张图（题面 + 作答），超出就只留前面这些。
    # 实验报告是整份 PDF 按页渲染的，一份十几页很常见，所以给到 16 张。
    llm_max_images: int = 16
    # 每次调用带上 usage，方便估成本
    llm_log_usage: bool = True

    # 上传
    max_upload_mb: int = 300

    # i学习 抓取（借本机 Edge 调试端口读取登录态）
    istudy_cdp_port: int = 9222

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
