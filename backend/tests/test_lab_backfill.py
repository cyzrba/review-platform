"""实验报告回填：预览、覆盖判断和真实字段写入。"""

from __future__ import annotations

import itertools

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Class,
    Course,
    FileState,
    GradingResult,
    IstudyLabReport,
    IstudyLabSubmission,
    ResultStatus,
    Rubric,
    Student,
    SubmissionState,
)
from app.services import backfill as backfill_service

_counter = itertools.count(1)


class FakeLabClient:
    scores = []
    comments = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def lab_update_score(self, fill_id, score, *, dept_id):
        type(self).scores.append((fill_id, score, dept_id))
        return {"code": 1000}

    async def lab_submit_comment(self, fill_id, total_score, comment, *, dept_id):
        type(self).comments.append((fill_id, total_score, comment, dept_id))
        return {"code": 1000}


def _create_lab_data() -> tuple[int, int, list[int], list[str]]:
    index = next(_counter)
    db = SessionLocal()
    try:
        course = Course(name=f"实验回填课程{index}")
        klass = Class(department="回填学院", major="物理", name=f"实验回填班{index}")
        rubric = Rubric(
            name=f"实验回填{index}",
            kind="lab_report",
            criteria="按报告质量评分",
            total_score=100,
        )
        db.add_all([course, klass, rubric])
        db.flush()
        report = IstudyLabReport(
            course_id=course.id,
            class_id=klass.id,
            rubric_id=rubric.id,
            istudy_cid=f"cid-{index}",
            istudy_report_id=f"report-{index}",
            istudy_class_id=f"class-{index}",
            name=f"实验报告{index}",
            report_type=2,
            total_count=2,
            submitted_count=2,
        )
        db.add(report)
        db.flush()
        student_ids = []
        fill_ids = []
        for offset, (score, istudy_score) in enumerate([(88.5, None), (75, 90)]):
            student = Student(
                class_id=klass.id,
                student_no=f"2027{index:04d}{offset:02d}",
                name=f"实验学生{offset}",
            )
            db.add(student)
            db.flush()
            fill_id = str(8100 + index * 10 + offset)
            student_ids.append(student.id)
            fill_ids.append(fill_id)
            db.add(
                IstudyLabSubmission(
                    report_id=report.id,
                    student_id=student.id,
                    istudy_fill_id=fill_id,
                    state=SubmissionState.submitted,
                    file_state=FileState.ready,
                    source_filename=f"{student.name}.pdf",
                    object_key=f"lab/{student.id}/report.pdf",
                    size_bytes=10,
                    istudy_score=istudy_score,
                )
            )
            db.add(
                GradingResult(
                    rubric_id=rubric.id,
                    student_id=student.id,
                    class_id=klass.id,
                    source_filename=f"{student.name}.pdf",
                    object_key=f"lab/{student.id}/report.pdf",
                    size_bytes=10,
                    status=ResultStatus.graded,
                    score=score,
                    comment=f"{student.name}的 AI 评语",
                )
            )
        db.commit()
        return report.id, rubric.id, student_ids, fill_ids
    finally:
        db.close()


def test_lab_backfill_preview_and_apply(client, monkeypatch):
    FakeLabClient.scores = []
    FakeLabClient.comments = []
    report_id, rubric_id, student_ids, fill_ids = _create_lab_data()
    monkeypatch.setattr(backfill_service, "IstudyClient", FakeLabClient)

    preview = client.post(f"/api/istudy/lab-reports/{report_id}/backfill/preview", json={})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["summary"] == {"total": 2, "write": 1, "skip": 1}
    skipped = [row for row in plan["rows"] if row["action"] == "skip"]
    assert "已有分数 90" in skipped[0]["reason"]

    overwrite = client.post(
        f"/api/istudy/lab-reports/{report_id}/backfill/preview",
        json={"overwrite": True},
    )
    assert overwrite.status_code == 200
    assert overwrite.json()["summary"]["write"] == 2

    applied = client.post(
        f"/api/istudy/lab-reports/{report_id}/backfill",
        json={"student_ids": student_ids, "overwrite": True, "write_comment": True},
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert {item[0] for item in FakeLabClient.scores} == set(fill_ids)
    assert {item[0] for item in FakeLabClient.comments} == set(fill_ids)
    expected_dept = settings.istudy_lab_dept_id or "1860"
    assert all(item[2] == expected_dept for item in FakeLabClient.scores)

    db = SessionLocal()
    try:
        rows = (
            db.execute(select(GradingResult).where(GradingResult.rubric_id == rubric_id))
            .scalars()
            .all()
        )
        assert all(row.istudy_backfill_status == "success" for row in rows)
        submissions = (
            db.execute(
                select(IstudyLabSubmission).where(
                    IstudyLabSubmission.student_id.in_(student_ids)
                )
            )
            .scalars()
            .all()
        )
        assert all(item.istudy_score is not None for item in submissions)
    finally:
        db.close()
