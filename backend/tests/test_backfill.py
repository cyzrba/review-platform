"""回填 i学习：表单解析、分数拆分、预览与逐人写入。"""

from __future__ import annotations

import io
import itertools
import zipfile

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Course,
    FileState,
    GradingResult,
    IstudySubmission,
    IstudyWork,
    Student,
    SubmissionState,
)
from app.services import backfill as backfill_service
from app.services.istudy import distribute_total, parse_mark_list, parse_review_form

_counter = itertools.count(1)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_parse_review_form_reads_hidden_fields():
    html = """
    <html><body>
    <form action="/mooc2-ans/work/library/save-review" id="form1" method="post">
      <input type="hidden" name="courseid" value="c1"/>
      <input type="hidden" name="score" id="score"/>
      <input type="hidden" name="qscore11" value="60"/>
      <input type="hidden" name="qscore12" value="40"/>
      <textarea id="comment1" name="comment">原评语 &amp; 说明</textarea>
    </form>
    </body></html>
    """
    fields = parse_review_form(html)
    assert fields["courseid"] == ["c1"]
    assert fields["score"] == [""]
    assert fields["qscore11"] == ["60"]
    assert fields["comment"] == ["原评语 & 说明"]


def test_distribute_total_keeps_sum():
    assert distribute_total(98, [100]) == [98]
    parts = distribute_total(98, [60, 40])
    assert round(sum(parts), 1) == 98
    assert distribute_total(0, [60, 40]) == [0.0, 0.0]


def test_parse_mark_list_reads_existing_score():
    html = """
    <ul class="dataBody_td" id="90000001" createid="18000000000001">
      <li class="dataBody_check"><input type="checkbox"/></li>
      <li class="taskBody_name"><div>张同学</div></li>
      <li>2026000001</li>
      <li>10-26 00:04</li>
      <li>1.2.3.4</li>
      <li>已完成（补交）</li>
      <li>王老师</li>
      <li><input class="inp80 scoreInput" type="text" value="88" data="88.0" /></li>
      <li>查看</li>
    </ul>
    """
    rows, _ = parse_mark_list(html)
    assert rows[0]["student_no"] == "2026000001"
    assert rows[0]["state"] == "已完成（补交）"
    assert rows[0]["istudy_score"] == "88"


class FakeIstudyClient:
    """假装成 IstudyClient：不发网络请求，只记录要写入的内容。"""

    state = "待批阅"
    saved: list[dict] = []
    students: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def list_submitted_students(self, cid, cpi, clazzid, work_id):
        return list(self.students)

    async def fetch_review_form(self, *, cid, cpi, clazzid, work_id, answer_id):
        qid = f"77{answer_id}"
        fields = {
            "courseid": [cid],
            "clazzid": [clazzid],
            "bbsid": ["bbs"],
            "mooc": ["1"],
            "isdisplaytable": ["1"],
            "isRework": ["false"],
            "isWork": ["false"],
            "workAnswerId": [answer_id],
            "workId": [work_id],
            "score": [""],
            "answerwqbid": [""],
            "firstHeader": ["2"],
            "enc": ["enc"],
            "grading": ["0"],
            "fullScore": ["100"],
            "cpi": [cpi],
            "markType": ["0"],
            "uploadtype": ["work"],
            "uploadTimeStamp": ["1"],
            "uploadEnc": ["x"],
            "groupId": ["0"],
            "status": ["0"],
            "pages": ["1"],
            "size": ["20"],
            f"qscore{qid}": ["100.0"],
            f"score{qid}": [""],
        }
        return fields, "https://mooc.example/review"

    async def save_review(self, fields, *, score, referer, comment=None, per_scores=None):
        type(self).saved.append(
            {
                "answer_id": fields["workAnswerId"][0],
                "score": score,
                "comment": comment,
                "per_scores": per_scores,
            }
        )
        return {"status": True, "msg": "success"}


def _setup(client) -> tuple[int, int, int, str, str, int]:
    index = next(_counter)
    department = f"回填学院{index}"
    classroom = f"回填班{index}"
    no_a = f"2026{index:04d}01"
    no_b = f"2026{index:04d}02"

    course = client.post(
        "/api/courses",
        json={"name": f"回填课程{index}", "code": f"BF{index:03d}", "term": "2026-2027-1"},
    )
    assert course.status_code == 201, course.text
    course_id = course.json()["id"]

    roster = (
        "学号,姓名,院系,专业,班级,加入时间,入学年份\n"
        f"{no_a},甲同学,{department},软件工程,{classroom},2026-09-01,2026\n"
        f"{no_b},乙同学,{department},软件工程,{classroom},2026-09-01,2026\n"
    ).encode("utf-8-sig")
    imported = client.post(
        "/api/classes/import",
        files={"file": ("roster.csv", roster, "text/csv")},
        data={"course_id": str(course_id)},
    )
    assert imported.status_code == 200, imported.text

    classes = client.get(
        f"/api/courses/{course_id}/classes", params={"page_size": 50}
    ).json()["items"]
    class_id = classes[0]["id"]

    rubric = client.post(
        "/api/rubrics",
        json={
            "name": f"回填作业{index}",
            "kind": "homework",
            "criteria": "内容完整 60 分，结论合理 40 分。",
            "total_score": 100,
            "course_ids": [course_id],
        },
    )
    assert rubric.status_code == 201, rubric.text
    rubric_id = rubric.json()["id"]

    upload = client.post(
        f"/api/rubrics/{rubric_id}/submissions",
        files={
            "files": (
                "homework.zip",
                _zip_bytes(
                    {
                        f"{department}-软件工程-{classroom}-{no_a}-甲同学.docx": b"a",
                        f"{department}-软件工程-{classroom}-{no_b}-乙同学.docx": b"b",
                    }
                ),
                "application/zip",
            )
        },
    )
    assert upload.status_code == 200, upload.text
    graded = client.post(
        f"/api/rubrics/{rubric_id}/grade", json={}, params={"sync": "true"}
    )
    assert graded.status_code == 200, graded.text
    return course_id, class_id, rubric_id, no_a, no_b, 0


def _insert_istudy_work(client, course_id, class_id, rubric_id, no_a, no_b):
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        course.istudy_cid = f"cid-{course_id}"
        course.istudy_cpi = f"cpi-{course_id}"
        work = IstudyWork(
            course_id=course_id,
            class_id=class_id,
            rubric_id=rubric_id,
            istudy_cid=course.istudy_cid,
            istudy_clazzid=f"clazz-{course_id}",
            istudy_task_id=f"task-{course_id}",
            istudy_work_id=f"work-{course_id}",
            name="回填测试作业",
        )
        db.add(work)
        db.flush()

        students = (
            db.execute(
                select(Student)
                .where(Student.class_id == class_id)
                .order_by(Student.student_no)
            )
            .scalars()
            .all()
        )
        answers = {}
        for offset, student in enumerate(students):
            answer_id = str(9000 + offset + course_id)
            answers[student.student_no] = answer_id
            db.add(
                IstudySubmission(
                    work_id=work.id,
                    student_id=student.id,
                    # 真实环境里历史导出链路不会回写这个字段，这里显式模拟空值
                    istudy_answer_id=None,
                    istudy_user_id=f"u-{student.student_no}",
                    state=SubmissionState.submitted,
                    file_state=FileState.ready,
                )
            )
        db.commit()
        work_id = work.id
        student_ids = [student.id for student in students]
        return work_id, student_ids, answers
    finally:
        db.close()


def _set_students(client, class_id, answers, states):
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(Student)
                .where(Student.class_id == class_id)
                .order_by(Student.student_no)
            )
            .scalars()
            .all()
        )
        FakeIstudyClient.students = [
            {
                "answer_id": answers[row.student_no],
                "istudy_user_id": f"u-{row.student_no}",
                "name": row.name,
                "student_no": row.student_no,
                "submitted_at": "10-26 10:00",
                "state": states.get(row.student_no, "待批阅"),
                "istudy_score": "0"
                if states.get(row.student_no, "待批阅") == "待批阅"
                else "84.5",
            }
            for row in rows
        ]
    finally:
        db.close()


def test_backfill_preview_and_apply(client, monkeypatch):
    FakeIstudyClient.saved = []
    course_id, class_id, rubric_id, no_a, no_b, _ = _setup(client)
    work_id, student_ids, answers = _insert_istudy_work(
        client, course_id, class_id, rubric_id, no_a, no_b
    )
    _set_students(
        client,
        class_id,
        answers,
        {no_a: "待批阅", no_b: "待批阅"},
    )
    monkeypatch.setattr(backfill_service, "IstudyClient", FakeIstudyClient)

    preview = client.post(f"/api/istudy/works/{work_id}/backfill/preview", json={})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["summary"] == {"total": 2, "write": 2, "skip": 0}
    assert {row["student_no"] for row in plan["rows"]} == {no_a, no_b}
    assert all(row["istudy_score"] is None for row in plan["rows"])

    applied = client.post(
        f"/api/istudy/works/{work_id}/backfill",
        json={"student_ids": student_ids, "write_comment": True},
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert len(FakeIstudyClient.saved) == 2
    assert all(item["comment"] for item in FakeIstudyClient.saved)

    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(GradingResult).where(GradingResult.rubric_id == rubric_id)
            )
            .scalars()
            .all()
        )
        assert all(row.istudy_backfill_status == "success" for row in rows)
        assert all(row.istudy_backfill_score is not None for row in rows)
        submissions = (
            db.execute(
                select(IstudySubmission).where(IstudySubmission.work_id == work_id)
            )
            .scalars()
            .all()
        )
        assert all(item.istudy_answer_id for item in submissions)
    finally:
        db.close()


def test_backfill_skips_graded_without_overwrite(client, monkeypatch):
    FakeIstudyClient.saved = []
    course_id, class_id, rubric_id, no_a, no_b, _ = _setup(client)
    work_id, student_ids, answers = _insert_istudy_work(
        client, course_id, class_id, rubric_id, no_a, no_b
    )
    _set_students(
        client,
        class_id,
        answers,
        {no_a: "已完成", no_b: "待批阅"},
    )
    monkeypatch.setattr(backfill_service, "IstudyClient", FakeIstudyClient)

    plan = client.post(
        f"/api/istudy/works/{work_id}/backfill/preview", json={}
    ).json()
    assert plan["summary"]["write"] == 1
    skipped = [row for row in plan["rows"] if row["action"] == "skip"]
    assert skipped and "已批阅" in skipped[0]["reason"]
    assert skipped[0]["istudy_score"] == 84.5
    assert "84.5" in skipped[0]["reason"]

    overwrite_plan = client.post(
        f"/api/istudy/works/{work_id}/backfill/preview", json={"overwrite": True}
    ).json()
    assert overwrite_plan["summary"]["write"] == 2
