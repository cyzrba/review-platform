"""ORM -> Pydantic 输出模型的组装。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Class,
    Course,
    CourseClass,
    GradingResult,
    ResultStatus,
    Rubric,
    Student,
)
from .schemas import (
    ClassOut,
    CourseBrief,
    CourseOut,
    GradingResultOut,
    RubricOut,
    RubricStats,
    StudentOut,
)


def course_briefs(links) -> list[CourseBrief]:  # noqa: ANN001
    return [
        CourseBrief(id=link.course.id, name=link.course.name)
        for link in links
        if link.course is not None
    ]


def course_out(db: Session, obj: Course) -> CourseOut:
    student_count = db.execute(
        select(func.count(func.distinct(Student.id)))
        .join(CourseClass, CourseClass.class_id == Student.class_id)
        .where(CourseClass.course_id == obj.id)
    ).scalar_one()
    return CourseOut(
        id=obj.id,
        name=obj.name,
        code=obj.code,
        term=obj.term,
        description=obj.description,
        created_at=obj.created_at,
        class_count=len(obj.class_links),
        student_count=student_count,
        rubric_count=len(obj.rubric_links),
    )


def class_out(obj: Class, *, student_count: int | None = None) -> ClassOut:
    return ClassOut(
        id=obj.id,
        name=obj.name,
        department=obj.department or "",
        major=obj.major or "",
        description=obj.description,
        full_name=obj.full_name,
        student_count=student_count if student_count is not None else len(obj.students),
        courses=course_briefs(obj.course_links),
        created_at=obj.created_at,
    )


def student_out(obj: Student) -> StudentOut:
    klass = obj.class_
    return StudentOut(
        id=obj.id,
        class_id=obj.class_id,
        class_name=klass.name if klass else None,
        department=klass.department if klass else None,
        major=klass.major if klass else None,
        student_no=obj.student_no,
        name=obj.name,
        joined_at=obj.joined_at,
        enrollment_year=obj.enrollment_year,
        email=obj.email,
        remark=obj.remark,
        created_at=obj.created_at,
    )


def rubric_stats(db: Session, rubric_id: int) -> RubricStats:
    total = db.execute(
        select(func.count(GradingResult.id)).where(GradingResult.rubric_id == rubric_id)
    ).scalar_one()
    rows = db.execute(
        select(GradingResult.status, func.count(GradingResult.id))
        .where(GradingResult.rubric_id == rubric_id)
        .group_by(GradingResult.status)
    ).all()
    counts = {status: count for status, count in rows}
    class_count = db.execute(
        select(func.count(func.distinct(GradingResult.class_id))).where(
            GradingResult.rubric_id == rubric_id
        )
    ).scalar_one()
    aggregate = db.execute(
        select(
            func.avg(GradingResult.score),
            func.max(GradingResult.score),
            func.min(GradingResult.score),
        ).where(GradingResult.rubric_id == rubric_id, GradingResult.score.is_not(None))
    ).one()

    graded = counts.get(ResultStatus.graded, 0)
    failed = counts.get(ResultStatus.failed, 0)
    pending = total - graded - failed
    return RubricStats(
        result_count=total,
        graded_count=graded,
        pending_count=max(pending, 0),
        failed_count=failed,
        class_count=class_count,
        average_score=round(float(aggregate[0]), 2) if aggregate[0] is not None else None,
        max_score=float(aggregate[1]) if aggregate[1] is not None else None,
        min_score=float(aggregate[2]) if aggregate[2] is not None else None,
    )


def rubric_out(db: Session, rubric: Rubric) -> RubricOut:
    return RubricOut(
        id=rubric.id,
        name=rubric.name,
        kind=rubric.kind,
        description=rubric.description,
        criteria=rubric.criteria,
        total_score=rubric.total_score,
        extra_prompt=rubric.extra_prompt,
        created_at=rubric.created_at,
        stats=rubric_stats(db, rubric.id),
        courses=course_briefs(rubric.course_links),
    )


def result_out(obj: GradingResult) -> GradingResultOut:
    klass = obj.class_
    return GradingResultOut(
        id=obj.id,
        rubric_id=obj.rubric_id,
        rubric_name=obj.rubric.name if obj.rubric else None,
        student_id=obj.student_id,
        student_no=obj.student.student_no if obj.student else None,
        student_name=obj.student.name if obj.student else None,
        class_id=obj.class_id,
        class_name=klass.name if klass else None,
        department=klass.department if klass else None,
        major=klass.major if klass else None,
        source_filename=obj.source_filename,
        object_key=obj.object_key,
        size_bytes=obj.size_bytes,
        match_reason=obj.match_reason,
        status=obj.status,
        score=obj.score,
        comment=obj.comment,
        model=obj.model,
        graded_at=obj.graded_at,
        duration_ms=obj.duration_ms,
        is_manual=obj.is_manual,
        error_message=obj.error_message,
        created_at=obj.created_at,
    )


RESULT_LOAD_OPTIONS = (
    selectinload(GradingResult.student),
    selectinload(GradingResult.class_),
    selectinload(GradingResult.rubric),
)
