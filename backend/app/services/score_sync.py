"""同步 i学习 作业已有的学生分数到本地 SQLite。

实验报告分数会在同步实验报告列表时一并写入；作业批阅分数需要单独读取
「按人批阅」列表。这里只更新 i学习 侧字段，不碰本地已下载的附件内容。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Course,
    FileState,
    IstudySubmission,
    IstudyWork,
    Student,
    SubmissionState,
)
from .istudy import IstudyClient

_SUBMITTED_AT = re.compile(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")


def _score_value(raw: str, state: str) -> float | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    # 「待批阅」时 i学习 可能回填 0，这表示还没有批阅，不是学生得了 0 分。
    if score == 0 and "待批" in state:
        return None
    return score


def _submitted_at(raw: str, *, now: datetime | None = None) -> datetime | None:
    match = _SUBMITTED_AT.search(raw or "")
    if not match:
        return None
    month, day, hour, minute = (int(value) for value in match.groups())
    now = now or datetime.now()
    candidates: list[datetime] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute))
        except ValueError:
            continue
    return min(candidates, key=lambda item: abs((item - now).total_seconds())) if candidates else None


async def sync_work_scores(
    db: Session,
    course: Course,
    *,
    cid: str,
    cpi: str,
) -> dict[str, Any]:
    """读取每班批阅名单，把答案 id、提交状态和已有分数写进本地。

    对名单里的本地学生都会保留一条提交记录，尚未提交的记为 missing。
    已有附件不会被清空，所以再次同步成绩不会触发重新下载。
    """
    works = list(
        db.execute(
            select(IstudyWork)
            .where(IstudyWork.course_id == course.id)
            .order_by(IstudyWork.id)
        )
        .scalars()
        .all()
    )
    stats: dict[str, Any] = {
        "work_count": len(works),
        "student_rows": 0,
        "submitted_rows": 0,
        "scored_rows": 0,
        "unmatched_rows": 0,
        "failures": [],
    }
    if not works:
        return stats

    students_by_class: dict[int, dict[str, Student]] = {}
    submissions_by_work: dict[int, dict[int, IstudySubmission]] = {}

    async with IstudyClient() as client:
        for work in works:
            if work.class_id is None:
                stats["failures"].append(f"{work.name}：本地没有匹配到班级")
                continue

            roster = students_by_class.get(work.class_id)
            if roster is None:
                roster = {
                    student.student_no: student
                    for student in db.execute(
                        select(Student).where(Student.class_id == work.class_id)
                    )
                    .scalars()
                    .all()
                }
                students_by_class[work.class_id] = roster
            if not roster:
                stats["failures"].append(f"{work.name}：本地班级还没有学生名单")
                continue

            existing = submissions_by_work.get(work.id)
            if existing is None:
                existing = {
                    row.student_id: row
                    for row in db.execute(
                        select(IstudySubmission).where(IstudySubmission.work_id == work.id)
                    )
                    .scalars()
                    .all()
                }
                submissions_by_work[work.id] = existing

            try:
                live_rows = await client.list_submitted_students(
                    cid,
                    cpi,
                    work.istudy_clazzid,
                    work.istudy_work_id,
                )
            except Exception as exc:  # noqa: BLE001
                stats["failures"].append(f"{work.name}：{exc}")
                continue

            live_by_no = {
                str(row.get("student_no") or ""): row
                for row in live_rows
                if row.get("student_no")
            }
            submitted_numbers: set[str] = set()
            for student_no, student in roster.items():
                row = live_by_no.get(student_no)
                submission = existing.get(student.id)
                if submission is None:
                    submission = IstudySubmission(
                        work_id=work.id,
                        student_id=student.id,
                        state=SubmissionState.missing,
                    )
                    db.add(submission)
                    existing[student.id] = submission

                stats["student_rows"] += 1
                if row is None:
                    # 只同步分数时不要把已下载附件清掉；没有附件时状态按未交处理。
                    submission.istudy_score = None
                    submission.istudy_answer_id = None
                    submission.istudy_user_id = None
                    if submission.file_state == FileState.none:
                        submission.state = SubmissionState.missing
                    continue

                submitted_numbers.add(student_no)
                state = str(row.get("state") or "")
                score = _score_value(str(row.get("istudy_score") or ""), state)
                submission.state = SubmissionState.submitted
                submission.istudy_answer_id = str(row.get("answer_id") or "") or None
                submission.istudy_user_id = str(row.get("istudy_user_id") or "") or None
                submission.submitted_at = (
                    _submitted_at(str(row.get("submitted_at") or ""))
                    or submission.submitted_at
                )
                submission.istudy_score = score
                stats["submitted_rows"] += 1
                if score is not None:
                    stats["scored_rows"] += 1

            stats["unmatched_rows"] += sum(
                1 for student_no in live_by_no if student_no not in roster
            )

    db.flush()
    return stats
