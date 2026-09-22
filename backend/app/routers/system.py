"""健康检查与首页统计。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Class, Course, GradingResult, ResultStatus, Rubric, Student
from ..schemas import DashboardOut, StorageHealth
from ..storage import get_storage

router = APIRouter(tags=["系统"])


def storage_health() -> StorageHealth:
    storage = get_storage()
    ok, detail = storage.health()
    return StorageHealth(
        backend=storage.name,
        endpoint=getattr(storage, "endpoint", None),
        bucket=storage.bucket,
        ok=ok,
        detail=detail,
    )


@router.get("/health", summary="健康检查")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(select(func.count(Class.id)))
    return {
        "status": "ok",
        "app": settings.app_name,
        "storage": storage_health().model_dump(),
        "reviewer": settings.ai_reviewer,
    }


@router.get("/dashboard", response_model=DashboardOut, summary="首页统计")
def dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    def count(model, *conditions) -> int:
        stmt = select(func.count(model.id))
        if conditions:
            stmt = stmt.where(*conditions)
        return db.execute(stmt).scalar_one()

    return DashboardOut(
        course_count=count(Course),
        class_count=count(Class),
        student_count=count(Student),
        rubric_count=count(Rubric),
        result_count=count(GradingResult),
        graded_count=count(GradingResult, GradingResult.status == ResultStatus.graded),
        pending_count=count(
            GradingResult,
            GradingResult.status.in_([ResultStatus.pending, ResultStatus.grading]),
        ),
        storage=storage_health(),
    )
