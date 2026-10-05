"""数据库连接与会话管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir(url: str) -> None:
    """sqlite 文件所在目录不存在时自动创建。"""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    raw_path = url[len(prefix) :]
    if raw_path in ("", ":memory:") or raw_path.startswith("file:"):
        return
    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    # timeout 放大一点：AI 评审在后台线程里并发写库时避免 database is locked
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)


if _is_sqlite:
    # journal_mode 会写进 SQLite 文件本身，所以这个开关只在第一次连接时生效
    _ALLOWED_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
    _journal_mode = settings.sqlite_journal_mode.strip().upper()
    if _journal_mode not in _ALLOWED_JOURNAL_MODES:
        _journal_mode = "DELETE"

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA journal_mode={_journal_mode}")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_db() -> None:
    """建表（幂等），先补齐新增列，再对老版本遗留的库做一次友好提示。"""
    from . import models  # noqa: F401  仅用于注册元数据

    _migrate_add_columns()
    _warn_if_legacy_schema()
    Base.metadata.create_all(bind=engine)


# 版本升级时新增的列。SQLite 的 ALTER TABLE ADD COLUMN 只支持加可空 / 带默认值的列，
# 这里新增的列都满足，所以可以直接补，不用删库。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "courses": {
        "istudy_cid": "VARCHAR(32)",
        "istudy_cpi": "VARCHAR(32)",
    },
    "rubrics": {
        "istudy_task_id": "VARCHAR(32)",
        "istudy_library_id": "VARCHAR(64)",
        "source": "VARCHAR(16) DEFAULT 'manual'",
    },
    "istudy_works": {
        "istudy_library_id": "VARCHAR(64)",
    },
    "istudy_exports": {
        "person_ids": "TEXT",
    },
    "istudy_submissions": {
        "content_checksum": "VARCHAR(64)",
    },
    "istudy_lab_reports": {
        "tip_text": "VARCHAR(32)",
    },
    "grading_results": {
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "images_sent": "INTEGER",
    },
}


def _migrate_add_columns() -> None:
    """给已经存在的表补上新增列（幂等）。"""
    if not _is_sqlite:
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {item["name"] for item in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column in present:
                continue
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))


# 当前版本每张表都应该有、而老版本没有的列，用来识别过期数据库
_EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "classes": ("department",),
    "students": ("enrollment_year",),
    "rubrics": ("criteria", "istudy_task_id", "istudy_library_id", "source"),
    "grading_results": ("object_key", "prompt_tokens", "completion_tokens", "images_sent"),
    "courses": ("istudy_cid", "istudy_cpi"),
    "istudy_works": ("istudy_library_id",),
    "istudy_exports": ("person_ids",),
    "istudy_submissions": ("content_checksum",),
}


def _warn_if_legacy_schema() -> None:
    """老库缺列时 create_all 不会补，这里提前说清楚，免得后面报 "no such column"。"""
    if not _is_sqlite:
        return
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return

    outdated: list[str] = []
    for table, expected in _EXPECTED_COLUMNS.items():
        if table not in existing_tables:
            continue
        columns = {item["name"] for item in inspector.get_columns(table)}
        outdated.extend(f"{table}.{column}" for column in expected if column not in columns)
    if outdated:
        raise RuntimeError(
            "检测到旧版本的数据库结构，缺少字段："
            + "、".join(outdated)
            + "。开发阶段请删除 SQLite 文件后重建（make clean-reset），"
            "或改用新的 DATABASE_URL。"
        )


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用的会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台任务等非请求场景使用的会话上下文。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def with_retry(action, *, attempts: int = 4, base_delay: float = 0.3, what: str = "数据库操作"):
    """SQLite 偶尔会 `database is locked`（多个后台任务 + 接口同时在写），退避重试几次就好。

    这个错误是「抢锁失败」，不是数据坏了，重试是安全的。
    """
    import time

    from sqlalchemy.exc import OperationalError

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return action()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            last = exc
            time.sleep(base_delay * (2**attempt))
    raise last  # pragma: no cover
