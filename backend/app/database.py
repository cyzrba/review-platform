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

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_db() -> None:
    """建表（幂等），并对老版本遗留的库做一次友好提示。"""
    from . import models  # noqa: F401  仅用于注册元数据

    _warn_if_legacy_schema()
    Base.metadata.create_all(bind=engine)


# 当前版本每张表都应该有、而老版本没有的列，用来识别过期数据库
_EXPECTED_COLUMNS = {
    "classes": "department",
    "students": "enrollment_year",
    "rubrics": "criteria",
    "grading_results": "object_key",
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
    for table, column in _EXPECTED_COLUMNS.items():
        if table not in existing_tables:
            continue
        columns = {item["name"] for item in inspector.get_columns(table)}
        if column not in columns:
            outdated.append(f"{table}.{column}")
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
