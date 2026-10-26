"""删除本地附件：文件、数据库状态、评分结果保留策略。"""

from __future__ import annotations

import itertools

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Class,
    Course,
    FileState,
    GradingResult,
    IstudySubmission,
    IstudySubmissionFile,
    IstudyWork,
    ResultStatus,
    Rubric,
    Student,
    SubmissionState,
)
from app.storage import get_storage

_counter = itertools.count(1)


def _make_submission(client, *, students: int = 1):
    index = next(_counter)
    storage = get_storage()
    db = SessionLocal()
    try:
        course = Course(name=f"附件课程{index}")
        klass = Class(department="附件学院", major="软工", name=f"附件班{index}")
        rubric = Rubric(
            name=f"附件作业{index}",
            kind="homework",
            criteria="按内容给分",
            total_score=100,
        )
        db.add_all([course, klass, rubric])
        db.flush()

        work = IstudyWork(
            course_id=course.id,
            class_id=klass.id,
            rubric_id=rubric.id,
            istudy_cid="cid",
            istudy_clazzid="clazz",
            istudy_task_id=f"task-{index}",
            istudy_work_id=f"work-{index}",
            name=f"第{index}次作业",
        )
        db.add(work)
        db.flush()

        submissions = []
        results = []
        for offset in range(students):
            student = Student(
                class_id=klass.id,
                student_no=f"2026{index:04d}{offset:02d}",
                name=f"学生{offset}",
            )
            db.add(student)
            db.flush()

            source_key = f"attachments/{index}/{student.id}/source.pdf"
            image_key = f"attachments/{index}/{student.id}/answer/01-a.jpg"
            storage.put_bytes(source_key, b"s" * 100, "application/pdf")
            storage.put_bytes(image_key, b"i" * 50, "image/jpeg")

            submission = IstudySubmission(
                work_id=work.id,
                student_id=student.id,
                istudy_answer_id=str(1000 + offset),
                state=SubmissionState.submitted,
                file_state=FileState.ready,
                source_filename="source.pdf",
                object_key=source_key,
                size_bytes=100,
                image_count=1,
            )
            db.add(submission)
            db.flush()
            db.add(
                IstudySubmissionFile(
                    submission_id=submission.id,
                    seq=1,
                    role="answer",
                    filename="a.jpg",
                    object_key=image_key,
                    content_type="image/jpeg",
                    size_bytes=50,
                )
            )
            result = GradingResult(
                rubric_id=rubric.id,
                student_id=student.id,
                class_id=klass.id,
                source_filename="source.pdf",
                object_key=source_key,
                status=ResultStatus.graded,
                score=90.0,
                comment="很好",
            )
            db.add(result)
            submissions.append(submission)
            results.append(result)

        db.commit()
        return {
            "work_id": work.id,
            "submission_ids": [item.id for item in submissions],
            "student_ids": [item.student_id for item in submissions],
            "result_ids": [item.id for item in results],
            "source_keys": [item.object_key for item in submissions],
        }
    finally:
        db.close()


def test_delete_submission_files_keeps_result(client):
    info = _make_submission(client)
    storage = get_storage()
    submission_id = info["submission_ids"][0]
    source_key = info["source_keys"][0]

    response = client.delete(f"/api/istudy/submissions/{submission_id}/files")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == 2
    assert body["freed_bytes"] == 150
    assert body["results_deleted"] == 0
    assert not storage.exists(source_key)

    db = SessionLocal()
    try:
        submission = db.get(IstudySubmission, submission_id)
        assert submission.object_key is None
        assert submission.file_state == FileState.none
        assert submission.image_count == 0
        assert submission.files == []
        result = db.get(GradingResult, info["result_ids"][0])
        assert result is not None and result.score == 90.0
    finally:
        db.close()


def test_delete_submission_files_can_remove_result(client):
    info = _make_submission(client)
    submission_id = info["submission_ids"][0]

    response = client.delete(
        f"/api/istudy/submissions/{submission_id}/files",
        params={"also_result": "true"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results_deleted"] == 1

    db = SessionLocal()
    try:
        assert db.get(GradingResult, info["result_ids"][0]) is None
    finally:
        db.close()


def test_delete_work_files_clears_every_submission(client):
    info = _make_submission(client, students=2)

    response = client.delete(f"/api/istudy/works/{info['work_id']}/files")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submissions"] == 2
    assert body["files"] == 4
    assert body["freed_bytes"] == 300

    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(IstudySubmission).where(
                    IstudySubmission.work_id == info["work_id"]
                )
            )
            .scalars()
            .all()
        )
        assert all(item.file_state == FileState.none for item in rows)
        results = (
            db.execute(
                select(GradingResult).where(
                    GradingResult.student_id.in_(info["student_ids"])
                )
            )
            .scalars()
            .all()
        )
        assert len(results) == 2  # 默认保留评分结果
    finally:
        db.close()
