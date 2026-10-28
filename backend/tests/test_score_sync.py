"""i学习 作业成绩同步：写入提交记录、分数和列表统计。"""

from __future__ import annotations

from uuid import uuid4

from app.database import SessionLocal
from sqlalchemy import select

from app.models import (
    Course,
    GradingResult,
    GradingKind,
    IstudyWork,
    ResultStatus,
    Rubric,
    Student,
)
from app.services import score_sync


class FakeIstudyClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_submitted_students(self, *_args):
        return [
            {
                "answer_id": "9001",
                "istudy_user_id": "8801",
                "name": "张三",
                "student_no": self.student_no,
                "submitted_at": "10-27 09:30",
                "state": "已完成",
                "istudy_score": "88",
            }
        ]


def test_sync_work_scores_persists_scores_and_list_stats(client, monkeypatch):
    tag = uuid4().hex[:8]
    no_zhang = f"2026{tag}01"
    no_li = f"2026{tag}02"
    course_response = client.post(
        "/api/courses",
        json={"name": f"成绩同步测试-{tag}", "term": "2026-1"},
    )
    assert course_response.status_code == 201, course_response.text
    course_id = course_response.json()["id"]
    roster = (
        "学号,姓名,院系,专业,班级,加入时间,入学年份\n"
        f"{no_zhang},张三,测试学院,软件工程,软工测试班,2026-09-01,2026\n"
        f"{no_li},李四,测试学院,软件工程,软工测试班,2026-09-01,2026\n"
    ).encode("utf-8-sig")
    imported = client.post(
        "/api/classes/import",
        files={"file": ("roster.csv", roster, "text/csv")},
        data={"course_id": str(course_id)},
    )
    assert imported.status_code == 200, imported.text
    classes = client.get(
        f"/api/courses/{course_id}/classes",
        params={"page_size": 20},
    ).json()["items"]
    class_id = classes[0]["id"]

    with SessionLocal() as db:
        course = db.get(Course, course_id)
        course.istudy_cid = "1001"
        course.istudy_cpi = "2002"
        db.add(
            IstudyWork(
                course_id=course_id,
                class_id=class_id,
                istudy_cid="1001",
                istudy_clazzid="3003",
                istudy_task_id="4004",
                istudy_library_id="5005",
                istudy_work_id="6006",
                name="第 1 次作业",
            )
        )
        db.commit()

    FakeIstudyClient.student_no = no_zhang
    monkeypatch.setattr(score_sync, "IstudyClient", FakeIstudyClient)

    response = client.post(
        "/api/istudy/works/sync-scores",
        json={"course_id": course_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["student_rows"] == 2
    assert body["submitted_rows"] == 1
    assert body["scored_rows"] == 1

    listed = client.get(
        "/api/istudy/works",
        params={"course_id": course_id},
    ).json()
    assert listed["items"][0]["score_count"] == 1
    assert listed["items"][0]["average_istudy_score"] == 88

    work_id = listed["items"][0]["classes"][0]["work_id"]
    submissions = client.get(f"/api/istudy/works/{work_id}/submissions").json()
    by_no = {item["student_no"]: item for item in submissions}
    assert by_no[no_zhang]["istudy_score"] == 88
    assert by_no[no_li]["istudy_score"] is None
    assert by_no[no_zhang]["ai_score"] is None
    assert by_no[no_zhang]["ai_result_status"] is None

    with SessionLocal() as db:
        student = db.execute(
            select(Student).where(Student.student_no == no_zhang)
        ).scalar_one()
        rubric = Rubric(
            name="第 1 次作业",
            kind=GradingKind.homework,
            source="istudy",
        )
        db.add(rubric)
        db.flush()
        rubric_id = rubric.id
        work = db.get(IstudyWork, work_id)
        work.rubric_id = rubric.id
        db.add(
            GradingResult(
                rubric_id=rubric.id,
                student_id=student.id,
                class_id=class_id,
                source_filename="张三.pdf",
                object_key="answer.pdf",
                status=ResultStatus.graded,
                score=93.5,
            )
        )
        db.commit()

    submissions = client.get(f"/api/istudy/works/{work_id}/submissions").json()
    by_no = {item["student_no"]: item for item in submissions}
    assert by_no[no_zhang]["ai_result_status"] == "graded"
    assert by_no[no_zhang]["ai_score"] == 93.5
    assert by_no[no_li]["ai_result_status"] is None
    assert by_no[no_li]["ai_score"] is None

    result_page = client.get(f"/api/rubrics/{rubric_id}/results").json()
    result = result_page["items"][0]
    assert result["istudy_score"] == 88
    assert result["score"] == 93.5
