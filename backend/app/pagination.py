"""统一的查询分页。

所有列表接口都用同一套参数和响应结构：

    请求：?page=1&page_size=20
    响应：{"items": [...], "total": 120, "page": 1, "page_size": 20, "pages": 6}
"""

from __future__ import annotations

from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}"
    ),
) -> PageParams:
    """FastAPI 依赖：解析分页参数。"""
    return PageParams(page=page, page_size=page_size)


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    pages: int = 0


def count_of(db: Session, stmt) -> int:  # noqa: ANN001
    return db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()


def fetch_page(db: Session, stmt, params: PageParams) -> tuple[list, int]:  # noqa: ANN001
    """返回 (当前页记录, 总条数)。"""
    total = count_of(db, stmt)
    rows = db.execute(stmt.offset(params.offset).limit(params.page_size)).scalars().all()
    return list(rows), total


def make_page(items: list, total: int, params: PageParams) -> Page:  # noqa: ANN001
    pages = (total + params.page_size - 1) // params.page_size if total else 0
    return Page(
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        pages=pages,
    )
