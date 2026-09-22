"""名单落库：手工上传名单与从 i学习 抓取名单共用同一套逻辑。

规则：
    * 班级按「院系 + 专业 + 班级名称」复用或新建，并自动挂到目标课程下；
    * 学号在班级内唯一，已存在则更新姓名等字段；
    * `clear_course_roster` 清空某课程名下的名单（班级、学生、评分结果）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Class, Course, CourseClass, GradingResult, Student


@dataclass
class SyncSummary:
    """一次名单导入 / 清空的结果统计。"""

    classes_created: int = 0
    classes_linked: int = 0
    students_created: int = 0
    students_updated: int = 0
    classes_removed: int = 0
    students_removed: int = 0
    results_removed: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)


def find_class(db: Session, department: str, major: str, name: str) -> Class | None:
    return db.execute(
        select(Class).where(
            Class.department == department, Class.major == major, Class.name == name
        )
    ).scalars().first()


def link_class_to_course(db: Session, course_id: int, class_id: int) -> bool:
    """把班级挂到课程下；已经挂过就返回 False。"""
    exists = db.execute(
        select(CourseClass.id).where(
            CourseClass.course_id == course_id, CourseClass.class_id == class_id
        )
    ).scalars().first()
    if exists is not None:
        return False
    db.add(CourseClass(course_id=course_id, class_id=class_id))
    return True


def clear_course_roster(db: Session, course: Course) -> SyncSummary:
    """清空某课程名下的名单。

    班级同时挂在别的课程下时只摘掉关联，不删班级本身；否则连同学生、
    评分结果一起删除。
    """
    summary = SyncSummary()
    links = list(
        db.execute(select(CourseClass).where(CourseClass.course_id == course.id)).scalars()
    )
    for link in links:
        klass = db.get(Class, link.class_id)
        if klass is None:
            db.delete(link)
            continue
        others = db.execute(
            select(func.count(CourseClass.id)).where(
                CourseClass.class_id == klass.id, CourseClass.course_id != course.id
            )
        ).scalar_one()
        if others:
            db.delete(link)
            continue
        summary.students_removed += db.execute(
            select(func.count(Student.id)).where(Student.class_id == klass.id)
        ).scalar_one()
        summary.results_removed += db.execute(
            select(func.count(GradingResult.id)).where(GradingResult.class_id == klass.id)
        ).scalar_one()
        summary.classes_removed += 1
        db.delete(klass)
    db.flush()
    return summary


def import_rows(
    db: Session,
    course: Course,
    rows: list[dict[str, Any]],
    *,
    replace: bool = False,
) -> SyncSummary:
    """把规范化后的名单行写入数据库。

    rows 里每项可含：student_no / name（必填）、department / major / class_name、
    joined_at / enrollment_year / email。
    """
    summary = SyncSummary()
    if replace:
        cleared = clear_course_roster(db, course)
        summary.classes_removed = cleared.classes_removed
        summary.students_removed = cleared.students_removed
        summary.results_removed = cleared.results_removed

    class_cache: dict[tuple[str, str, str], Class] = {}
    for row in rows:
        student_no = (row.get("student_no") or "").strip()
        name = (row.get("name") or "").strip()
        if not student_no or not name:
            summary.skipped.append(
                {"student_no": student_no, "reason": "缺少学号或姓名"}
            )
            continue

        department = (row.get("department") or "").strip()
        major = (row.get("major") or "").strip()
        class_name = (row.get("class_name") or "").strip()
        if not class_name:
            summary.skipped.append({"student_no": student_no, "reason": "名单里缺少班级名称"})
            continue

        key = (department, major, class_name)
        klass = class_cache.get(key)
        if klass is None:
            klass = find_class(db, department, major, class_name)
            if klass is None:
                klass = Class(name=class_name, department=department, major=major)
                db.add(klass)
                db.flush()
                summary.classes_created += 1
            if link_class_to_course(db, course.id, klass.id):
                summary.classes_linked += 1
            class_cache[key] = klass

        existing = db.execute(
            select(Student).where(
                Student.class_id == klass.id, Student.student_no == student_no
            )
        ).scalars().first()

        payload = {
            "name": name,
            "joined_at": row.get("joined_at"),
            "enrollment_year": row.get("enrollment_year"),
            "email": row.get("email"),
        }
        if existing is None:
            db.add(Student(class_id=klass.id, student_no=student_no, **payload))
            summary.students_created += 1
            continue

        changed = False
        for attr, value in payload.items():
            if value is not None and getattr(existing, attr) != value:
                setattr(existing, attr, value)
                changed = True
        if changed:
            summary.students_updated += 1

    db.flush()
    return summary
