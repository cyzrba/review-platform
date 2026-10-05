"""i学习 实验报告：同步报告名单 + 抓学生作答 PDF 入库。

抓取（不碰数据库）
    scrape_course_reports   课程 → 实验报告列表 → 按班级拆成一条条报告

落库
    apply_reports           写进 istudy_lab_reports + 学生名单 + 自动建评分细则

抓附件（i学习 是同步返回 zip，但一个班几十人也得等半分钟，所以放后台线程）
    enqueue_report_export   在请求里起一个后台任务，前端轮询 sync_state
    run_report_export       后台线程：下载 zip → 解包 → 抽作答图片 → 匹配学生 → 入库

磁盘结构（跟作业那边保持一致，按班级分文件夹）：
    istudy/lab-reports/25人工智能本1-超声波声速测量实验/
        exports/                    整包 zip，抓完可以删
        students/250410247/         学生交的 PDF
        students/250410247/answer/  从 PDF 里抠出来的作答图片，直接喂 AI
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, with_retry
from ..models import (
    Course,
    CourseClass,
    CourseRubric,
    FileState,
    GradingKind,
    GradingResult,
    IstudyLabFile,
    IstudyLabReport,
    IstudyLabSubmission,
    LabSyncState,
    ResultStatus,
    Rubric,
    Student,
    SubmissionState,
    WorkStatus,
)
from ..storage import get_storage
from ..utils import guess_content_type, safe_filename, sha256_hex
from . import archive
from .answer_files import AnswerFileError, extract_lab_report
from .homework_sync import _content_fingerprint
from .istudy import IstudyClient, IstudyError

logger = logging.getLogger(__name__)

LAB_FOLDER = "istudy/lab-reports"

# 自动建的细则：实验报告的通用评分口径，老师以后可以在「评分细则」页改
LAB_CRITERIA = (
    "1. 实验目的与原理（20 分）：写清楚本次实验要验证/测量的内容，原理公式与依据正确。\n"
    "2. 实验步骤与数据记录（30 分）：步骤可复现，原始数据（表格、读数、单位）记录完整。\n"
    "3. 数据处理与误差分析（30 分）：计算过程正确，图表规范，对误差来源有具体分析。\n"
    "4. 结论与规范（20 分）：结论与数据对应，量纲、有效数字、作图规范，报告完整。\n"
)

# i学习 的实验报告作答状态：0 未查看 / 1 已查看 / 2 已保存 / 3 待批阅 / 4 已批阅 / 5 待重做
LAB_STATE_BY_CODE: dict[int, SubmissionState] = {
    0: SubmissionState.missing,
    1: SubmissionState.missing,
    2: SubmissionState.draft,
    3: SubmissionState.submitted,
    4: SubmissionState.submitted,
    5: SubmissionState.submitted,
    99: SubmissionState.missing,
}

def _clean(value: Any) -> str:
    return str(value or "").strip().lstrip("\u200b")


def _split_ids(value: Any) -> list[str]:
    """noticeMoocClassIds 可能是 "18600000054836,18600000054797"。"""
    raw = _clean(value)
    if not raw:
        return []
    for sep in ("，", ",", "、"):
        raw = raw.replace(sep, ";")
    return [item.strip() for item in raw.split(";") if item.strip()]


def parse_lab_time(value: Any) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "").split(".")[0])
    except ValueError:
        return None


def parse_lab_json_list(value: Any) -> list[dict[str, Any]]:
    """i学习 的 uploadFiles 是一个「装着 JSON 字符串的字符串」。"""
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
@dataclass
class ScrapedLabStudent:
    student_no: str
    name: str
    state: SubmissionState
    submitted_at: datetime | None = None
    fill_id: str | None = None
    istudy_user_id: str | None = None
    istudy_score: float | None = None
    has_file: bool = False


@dataclass
class ScrapedLabReport:
    """一条「实验报告 × 班级」。"""

    istudy_report_id: str
    istudy_class_id: str
    class_name: str
    name: str
    report_type: int = 2
    status: int = 1
    tip_text: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    dept_id: str = "1860"
    students: list[ScrapedLabStudent] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.students)

    @property
    def submitted_count(self) -> int:
        return sum(1 for item in self.students if item.state == SubmissionState.submitted)

    @property
    def unsubmitted_count(self) -> int:
        return self.total_count - self.submitted_count


@dataclass
class LabScrapeResult:
    cid: str
    cpi: str = ""
    course_name: str = ""
    reports: list[ScrapedLabReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _students_from_report(item: dict[str, Any]) -> tuple[list[ScrapedLabStudent], dict[str, int]]:
    """把一条报告的 stuList 拆成学生列表，顺带统计各个状态的个数。"""
    students: list[ScrapedLabStudent] = []
    counter: dict[str, int] = {}
    for row in item.get("stuList") or []:
        student_no = _clean(row.get("belongNo"))
        if not student_no:
            continue
        raw_status = row.get("status")
        try:
            code = int(raw_status) if raw_status is not None else 0
        except (TypeError, ValueError):
            code = 0
        counter[str(code)] = counter.get(str(code), 0) + 1
        files = parse_lab_json_list(row.get("uploadFiles"))
        files += parse_lab_json_list(row.get("currUploadFiles"))
        score = row.get("score")
        students.append(
            ScrapedLabStudent(
                student_no=student_no,
                name=_clean(row.get("belongUname")),
                state=LAB_STATE_BY_CODE.get(code, SubmissionState.missing),
                submitted_at=parse_lab_time(row.get("submitTime")),
                fill_id=_clean(row.get("id")) or None,
                istudy_user_id=_clean(row.get("belongUid")) or None,
                istudy_score=float(score) if isinstance(score, (int, float)) else None,
                has_file=bool(files),
            )
        )
    return students, counter


async def scrape_course_reports(
    cid: str, cpi: str = "", *, course_name: str = ""
) -> LabScrapeResult:
    """抓一门课的实验报告列表，按班级展开成一条条记录。"""
    result = LabScrapeResult(cid=cid, cpi=cpi, course_name=course_name)
    async with IstudyClient() as client:
        dept_id = await client.lab_dept_id(cid, cpi) if cpi else "1860"
        rows = await client.lab_reports(cid, dept_id=dept_id)

    for item in rows:
        report_id = _clean(item.get("id"))
        if not report_id:
            continue
        class_ids = _split_ids(item.get("noticeMoocClassIds"))
        class_names = _split_ids(item.get("noticeMoocClassNames"))
        students, _counter = _students_from_report(item)
        if not class_ids:
            # 没指定班级（发给指定学生之类）：当成一条没有班级归属的记录，先跳过
            result.notes.append(f"报告「{_clean(item.get('name'))}」没有班级，已跳过")
            continue
        # 一个报告记录可能同时发给好几个班：先按「报告 × 班级」各存一条，
        # 具体哪个学生属于哪个班，入库时按本地名单再分一次。
        for index, class_id in enumerate(class_ids):
            class_name = class_names[index] if index < len(class_names) else ""
            result.reports.append(
                ScrapedLabReport(
                    istudy_report_id=report_id,
                    istudy_class_id=class_id,
                    class_name=class_name,
                    name=_clean(item.get("name")),
                    report_type=int(item.get("type") or 2),
                    status=int(item.get("status") or 1),
                    tip_text=_clean(item.get("tipText")),
                    start_at=parse_lab_time(item.get("startTime")),
                    end_at=parse_lab_time(item.get("endTime")),
                    dept_id=dept_id,
                    students=list(students),
                )
            )

    return result


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def lab_folder(report: IstudyLabReport) -> str:
    """对象存储里这次实验报告的文件夹名，例如 ``25人工智能本1-超声波声速测量实验``。"""
    class_name = report.class_.name if report.class_ else ""
    label = f"{class_name}-{report.name}" if class_name else report.name
    return safe_filename(label, fallback=f"lab-{report.id}")


def report_prefix(report: IstudyLabReport) -> str:
    return f"{LAB_FOLDER}/{lab_folder(report)}"


def local_class_id(db: Session, course: Course, class_name: str) -> int | None:
    from ..models import Class

    row = (
        db.execute(
            select(Class)
            .join(CourseClass, CourseClass.class_id == Class.id)
            .where(CourseClass.course_id == course.id, Class.name == class_name)
        )
        .scalars()
        .first()
    )
    return row.id if row else None


def ensure_lab_rubric(db: Session, course: Course, *, report_id: str, name: str) -> Rubric:
    """一次实验报告 = 一份评分细则。

    同一次实验会拆成好几个报告记录（每班一条，reportId 不同但名字相同），
    所以用 ``istudy_library_id = "lab:实验名"`` 认它们，别按 reportId 建重复的。
    """
    key = f"lab:{name}"
    rubric = (
        db.execute(select(Rubric).where(Rubric.istudy_library_id == key)).scalars().first()
    )
    if rubric is None:
        rubric = Rubric(
            name=name,
            kind=GradingKind.lab_report,
            description=None,
            criteria=LAB_CRITERIA,
            total_score=100.0,
            source="istudy-lab",
            istudy_task_id=f"lab:{report_id}",
            istudy_library_id=key,
        )
        db.add(rubric)
        db.flush()
    linked = (
        db.execute(
            select(CourseRubric.id).where(
                CourseRubric.course_id == course.id, CourseRubric.rubric_id == rubric.id
            )
        )
        .scalars()
        .first()
    )
    if linked is None:
        db.add(CourseRubric(course_id=course.id, rubric_id=rubric.id))
        db.flush()
    return rubric


def report_group(db: Session, report: IstudyLabReport) -> list[IstudyLabReport]:
    """「这次实验」包含的所有班级行（名字相同、reportId 不同的那些）。"""
    rows = (
        db.execute(
            select(IstudyLabReport).where(
                IstudyLabReport.course_id == report.course_id,
                IstudyLabReport.name == report.name,
            )
        )
        .scalars()
        .all()
    )
    return list(rows) or [report]


# i学习 列表页在报告名旁边挂的角标就是作答状态。
# 老师会先把报告预填好（未开始），再点发布（已开始），到期变已截止。
LAB_STATUS_BY_TIP = {
    "未开始": WorkStatus.not_started,
    "已开始": WorkStatus.ongoing,
    "进行中": WorkStatus.ongoing,
    "已截止": WorkStatus.ended,
    "已结束": WorkStatus.ended,
}


def report_status(row: IstudyLabReport, *, now: datetime | None = None) -> WorkStatus:
    """实验报告的作答状态。

    以 i学习 页面上的角标（``tipText``）为准：未开始 / 已开始 / 已截止。
    别拿开始时间自己算 —— 预填好但还没发布的报告也会带开始时间，用时间算会误判成进行中。
    老数据没有 tipText 时才退回按时间估。
    """
    tip = (row.tip_text or "").strip()
    if tip in LAB_STATUS_BY_TIP:
        return LAB_STATUS_BY_TIP[tip]
    now = now or datetime.now()
    if row.start_at and now < row.start_at:
        return WorkStatus.not_started
    if row.end_at and now > row.end_at:
        return WorkStatus.ended
    return WorkStatus.ongoing


def list_lab_report_groups(
    db: Session,
    course_id: int,
    *,
    class_id: int | None = None,
    status: WorkStatus | None = None,
) -> list[dict[str, Any]]:
    """按「实验名」聚合成列表页要显示的一行行，班级明细挂在 classes 里。"""
    from sqlalchemy import case, func

    stmt = select(IstudyLabReport).where(IstudyLabReport.course_id == course_id)
    if class_id is not None:
        stmt = stmt.where(IstudyLabReport.class_id == class_id)
    reports = list(db.execute(stmt.order_by(IstudyLabReport.id)).scalars().all())
    now = datetime.now()
    if status is not None:
        # 状态是从 i学习 的角标推出来的，只能在 Python 里过滤；
        # 过滤要在聚合之前做，这样分组上的「已交 / 已抓」也是这个状态下的口径。
        reports = [item for item in reports if report_status(item, now=now) == status]
    if not reports:
        return []

    report_ids = [item.id for item in reports]
    submission_stats = {
        row[0]: row[1:]
        for row in db.execute(
            select(
                IstudyLabSubmission.report_id,
                func.sum(case((IstudyLabSubmission.file_state == FileState.ready, 1), else_=0)),
                func.sum(case((IstudyLabSubmission.state == SubmissionState.missing, 1), else_=0)),
                func.sum(
                    case(
                        (
                            IstudyLabSubmission.file_state == FileState.ready,
                            IstudyLabSubmission.image_count,
                        ),
                        else_=0,
                    )
                ),
            )
            .where(IstudyLabSubmission.report_id.in_(report_ids))
            .group_by(IstudyLabSubmission.report_id)
        )
    }

    rubric_ids = {item.rubric_id for item in reports if item.rubric_id}
    result_stats: dict[tuple[int, int | None], tuple[int, int]] = {}
    if rubric_ids:
        for rubric_id, row_class_id, total, graded in db.execute(
            select(
                GradingResult.rubric_id,
                GradingResult.class_id,
                func.count(GradingResult.id),
                func.sum(case((GradingResult.status == ResultStatus.graded, 1), else_=0)),
            )
            .where(GradingResult.rubric_id.in_(rubric_ids))
            .group_by(GradingResult.rubric_id, GradingResult.class_id)
        ):
            result_stats[(rubric_id, row_class_id)] = (total or 0, int(graded or 0))

    groups: dict[str, dict[str, Any]] = {}
    for report in reports:
        captured, missing, images = submission_stats.get(report.id, (0, 0, 0))
        results, graded = result_stats.get((report.rubric_id or -1, report.class_id), (0, 0))
        row_status = report_status(report, now=now)
        class_row = {
            "source": "lab",
            "work_id": report.id,
            "class_id": report.class_id,
            "class_name": report.class_.name if report.class_ else "",
            "rubric_id": report.rubric_id,
            "istudy_clazzid": report.istudy_class_id,
            "istudy_work_id": report.istudy_report_id,
            "report_type": report.report_type,
            "status": row_status,
            "start_at": report.start_at,
            "end_at": report.end_at,
            "submitted_count": report.submitted_count,
            "unsubmitted_count": report.unsubmitted_count,
            "pending_count": 0,
            "captured_count": int(captured or 0),
            "missing_count": int(missing or 0),
            "image_count": int(images or 0),
            "result_count": results,
            "graded_count": int(graded or 0),
            "last_export": None,
            "sync_state": report.sync_state.value,
            "sync_message": report.sync_message,
        }

        group = groups.get(report.name)
        if group is None:
            group = {
                "source": "lab",
                "name": report.name,
                "library_id": f"lab:{report.name}",
                "task_ids": [],
                "rubric_ids": [],
                "status_breakdown": {},
                "start_at": None,
                "end_at": None,
                "rubric_id": report.rubric_id,
                "submitted_count": 0,
                "unsubmitted_count": 0,
                "pending_count": 0,
                "captured_count": 0,
                "missing_count": 0,
                "image_count": 0,
                "result_count": 0,
                "graded_count": 0,
                "classes": [],
            }
            groups[report.name] = group

        if report.istudy_report_id not in group["task_ids"]:
            group["task_ids"].append(report.istudy_report_id)
        if report.rubric_id and report.rubric_id not in group["rubric_ids"]:
            group["rubric_ids"].append(report.rubric_id)
        key = row_status.value
        group["status_breakdown"][key] = group["status_breakdown"].get(key, 0) + 1
        if report.start_at and (group["start_at"] is None or report.start_at < group["start_at"]):
            group["start_at"] = report.start_at
        if report.end_at and (group["end_at"] is None or report.end_at > group["end_at"]):
            group["end_at"] = report.end_at
        group["submitted_count"] += report.submitted_count
        group["unsubmitted_count"] += report.unsubmitted_count
        group["captured_count"] += class_row["captured_count"]
        group["missing_count"] += class_row["missing_count"]
        group["image_count"] += class_row["image_count"]
        group["result_count"] += results
        group["graded_count"] += int(graded or 0)
        group["classes"].append(class_row)

    items: list[dict[str, Any]] = []
    for group in groups.values():
        group["class_count"] = len(group["classes"])
        breakdown = group["status_breakdown"]
        group["status"] = WorkStatus(next(iter(breakdown))) if len(breakdown) == 1 else None
        group["classes"].sort(key=lambda item: (item["class_name"] or ""))
        items.append(group)

    items.sort(key=lambda item: (item["end_at"] or item["start_at"] or datetime.min), reverse=True)
    return items


def apply_reports(db: Session, course: Course, scrape: LabScrapeResult) -> dict[str, int]:
    """把抓到的实验报告写进库（按「报告 id + 班级」幂等更新）。"""
    created = updated = 0
    now = datetime.now()
    if scrape.cid and course.istudy_cid != scrape.cid:
        course.istudy_cid = scrape.cid
    if scrape.cpi and course.istudy_cpi != scrape.cpi:
        course.istudy_cpi = scrape.cpi

    for item in scrape.reports:
        class_id = local_class_id(db, course, item.class_name) if item.class_name else None
        if item.class_name and class_id is None:
            # 本地没有这个班的名单，抓了也没法匹配学生
            continue
        rubric = ensure_lab_rubric(db, course, report_id=item.istudy_report_id, name=item.name)
        row = (
            db.execute(
                select(IstudyLabReport).where(
                    IstudyLabReport.istudy_report_id == item.istudy_report_id,
                    IstudyLabReport.istudy_class_id == item.istudy_class_id,
                )
            )
            .scalars()
            .first()
        )
        payload = {
            "course_id": course.id,
            "class_id": class_id,
            "rubric_id": rubric.id,
            "istudy_cid": scrape.cid,
            "istudy_dept_id": item.dept_id,
            "name": item.name,
            "report_type": item.report_type,
            "istudy_status": item.status,
            "tip_text": item.tip_text or None,
            "start_at": item.start_at,
            "end_at": item.end_at,
            "last_synced_at": now,
        }
        if row is None:
            row = IstudyLabReport(
                istudy_report_id=item.istudy_report_id,
                istudy_class_id=item.istudy_class_id,
                **payload,
            )
            db.add(row)
            db.flush()
            created += 1
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            updated += 1

        _sync_report_students(db, row, item)
        # 一个报告记录可能覆盖好几个班，所以人数要按「这个班里实际有记录的提交」来数
        states = [
            value
            for (value,) in db.execute(
                select(IstudyLabSubmission.state).where(IstudyLabSubmission.report_id == row.id)
            ).all()
        ]
        row.total_count = len(states)
        row.submitted_count = sum(1 for state in states if state == SubmissionState.submitted)
        row.unsubmitted_count = row.total_count - row.submitted_count

    db.flush()
    return {"created": created, "updated": updated}


def _sync_report_students(db: Session, row: IstudyLabReport, item: ScrapedLabReport) -> None:
    """把 i学习 上的学生名单（谁交了、谁没交）同步到本地提交记录。

    只动「状态和 fillId」这些 i学习 侧的字段，本地抓下来的文件不动，
    这样重复同步不会把已经抓好的附件冲掉。
    """
    if row.class_id is None:
        return
    students = {
        student.student_no: student
        for student in db.execute(select(Student).where(Student.class_id == row.class_id))
        .scalars()
        .all()
    }
    existing = {
        submission.student_id: submission
        for submission in db.execute(
            select(IstudyLabSubmission).where(IstudyLabSubmission.report_id == row.id)
        )
        .scalars()
        .all()
    }
    istudy_by_no = {entry.student_no: entry for entry in item.students}

    for student_no, student in students.items():
        entry = istudy_by_no.get(student_no)
        submission = existing.get(student.id)
        if submission is None:
            submission = IstudyLabSubmission(report_id=row.id, student_id=student.id)
            db.add(submission)
            existing[student.id] = submission
        if entry is None:
            submission.state = SubmissionState.missing
            submission.istudy_fill_id = None
            submission.submitted_at = None
            continue
        submission.state = entry.state
        submission.istudy_fill_id = entry.fill_id
        submission.istudy_user_id = entry.istudy_user_id
        submission.submitted_at = entry.submitted_at
        submission.istudy_score = entry.istudy_score
    db.flush()


# --------------------------------------------------------------------------- #
# 抓附件
# --------------------------------------------------------------------------- #
def _targets(
    report: IstudyLabReport, *, student_ids: list[str] | None = None, force: bool = False
) -> list[IstudyLabSubmission]:
    """算出这次要导出哪几个学生。

    - 传了 student_ids：只抓这几个人（前端「按人抓取」勾选的）
    - 没传：默认只抓「还没抓过文件的」，force=True 才连抓过的也重抓一遍
    """
    submissions = [
        item
        for item in report.submissions
        if item.state == SubmissionState.submitted and item.istudy_fill_id
    ]
    if student_ids:
        wanted = {str(value) for value in student_ids}
        return [
            item
            for item in submissions
            if str(item.student_id) in wanted or str(item.istudy_fill_id) in wanted
        ]
    if force:
        return submissions
    return [item for item in submissions if item.file_state != FileState.ready]


def start_report_export(
    db: Session,
    report: IstudyLabReport,
    *,
    student_ids: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """标成「抓取中」并算出这次要导出的 fillId，交给后台线程去跑。"""
    targets = _targets(report, student_ids=student_ids, force=force)
    if not targets:
        return {
            "queued": 0,
            "fill_ids": [],
            "message": "没有需要抓的作答（都抓过了，或这次实验没人交）",
        }

    report.sync_state = LabSyncState.running
    report.sync_started_at = datetime.now()
    report.sync_finished_at = None
    report.sync_message = f"准备抓取 {len(targets)} 名学生的作答"
    db.commit()
    return {
        "queued": len(targets),
        "fill_ids": [str(item.istudy_fill_id) for item in targets if item.istudy_fill_id],
        "message": report.sync_message,
    }


def _upsert_grading_result(
    db: Session,
    *,
    rubric: Rubric,
    student: Student,
    class_id: int | None,
    name: str,
    size_bytes: int,
    checksum: str | None,
    object_key: str,
    match_reason: str,
) -> None:
    """登记成一条待评审记录。内容没变就保留原来的评审结果。"""
    row = (
        db.execute(
            select(GradingResult).where(
                GradingResult.rubric_id == rubric.id,
                GradingResult.student_id == student.id,
            )
        )
        .scalars()
        .first()
    )
    payload = {
        "source_filename": name,
        "object_key": object_key,
        "content_type": guess_content_type(name),
        "size_bytes": size_bytes,
        "checksum": checksum,
        "match_reason": match_reason,
    }
    if row is None:
        db.add(
            GradingResult(
                rubric_id=rubric.id,
                student_id=student.id,
                class_id=class_id or student.class_id,
                status=ResultStatus.pending,
                **payload,
            )
        )
        return
    previous_checksum = row.checksum
    for key, value in payload.items():
        setattr(row, key, value)
    file_changed = (
        previous_checksum is None or checksum is None or previous_checksum != checksum
    )
    if file_changed or row.status != ResultStatus.graded:
        row.status = ResultStatus.pending
        row.score = None
        row.comment = None
        row.error_message = None
        row.model = None
        row.graded_at = None
        row.is_manual = False


def persist_report_bundle(
    report_id: int, data: bytes, *, zip_name: str | None = None
) -> dict[str, Any]:
    """把下载下来的 zip 落进对象存储，并拆成一个个学生的作答。

    自己开一个会话：下载那一步已经跑完了，这里只做「解包 + 落盘 + 写库」，
    而且每个学生一个短事务，尽快把 SQLite 的写锁放掉。
    """
    db = SessionLocal()
    try:
        report = db.get(IstudyLabReport, report_id)
        if report is None:
            raise IstudyError(f"实验报告 {report_id} 不存在了")
        course = report.course
        rubric = report.rubric or ensure_lab_rubric(
            db, course, report_id=report.istudy_report_id, name=report.name
        )
        report.rubric_id = rubric.id

        bundle_name = zip_name or f"{report.name}.zip"
        prefix = report_prefix(report)
        export_key = f"{prefix}/exports/{safe_filename(bundle_name)}"
        get_storage().put_bytes(export_key, data, "application/zip")

        class_id = report.class_id
        rubric_id = rubric.id
        students = (
            db.execute(select(Student).where(Student.class_id == class_id)).scalars().all()
            if class_id
            else []
        )
        summary: dict[str, Any] = {"matched": 0, "images": 0, "issues": []}
        if not students:
            summary["issues"].append("这个班在本地还没有学生名单，先导入名单再来抓实验报告")
            return summary

        student_by_id = {student.id: student for student in students}
        class_name = report.class_.name if report.class_ else ""
        refs = [
            archive.StudentRef(
                id=student.id,
                student_no=student.student_no,
                name=student.name,
                class_name=class_name,
            )
            for student in students
        ]

        scan = archive.scan_uploads([(bundle_name, data)])
        leaves = [
            leaf for leaf in scan.leaves if "批注" not in " ".join([*leaf.parents, leaf.path])
        ]
        matched, issues = archive.assign_students(leaves, refs)
        summary["issues"] = [f"{item.filename}：{item.reason}" for item in issues[:20]]

        # 读到这里就结束事务：后面每个学生都自己起一个短事务，
        # 别让写锁一直攥在手里（好几个人同时抓的时候会互相把锁等死）
        db.commit()
    finally:
        db.close()

    storage = get_storage()
    for entry in matched:
        student = student_by_id.get(entry.student_id)
        if student is None:
            continue
        leaf = entry.leaf
        base = f"{prefix}/students/{safe_filename(student.student_no)}"
        source_key = f"{base}/{safe_filename(leaf.name)}"

        # 文件先落盘 + 抽作答图，这一段不碰数据库，慢一点也没关系
        storage.delete_prefix(base)
        storage.put_bytes(source_key, leaf.data, guess_content_type(leaf.name))
        try:
            answer = extract_lab_report(leaf.name, leaf.data)
        except AnswerFileError as exc:
            answer = None
            summary["issues"].append(f"{student.name}：{exc}")
            content_checksum = sha256_hex(leaf.data)
            image_count, extracted_text, error_message = 0, None, str(exc)
        else:
            content_checksum = _content_fingerprint(answer)
            image_count, extracted_text, error_message = len(answer.images), answer.text, None
            summary["images"] += len(answer.images)
            if answer.note:
                summary["issues"].append(f"{student.name}：{answer.note}")

        # 图片先写进对象存储（磁盘 IO），别放在数据库事务里做，免得把写锁占着
        image_rows: list[tuple[int, str, str, str, int, str]] = []
        for image in answer.images if answer is not None else []:
            image_key = f"{base}/answer/{image.seq:02d}-{safe_filename(image.filename)}"
            storage.put_bytes(image_key, image.data, image.content_type)
            image_rows.append(
                (
                    image.seq,
                    image.filename,
                    image_key,
                    image.content_type,
                    len(image.data),
                    sha256_hex(image.data),
                )
            )

        # 再把这一条写进库：独立的短事务 + 抢锁自动重试
        with_retry(
            lambda: _save_student_submission(
                report_id=report_id,
                rubric_id=rubric_id,
                class_id=class_id,
                student_id=student.id,
                source_filename=leaf.name,
                source_key=source_key,
                size_bytes=len(leaf.data),
                checksum=sha256_hex(leaf.data),
                content_checksum=content_checksum,
                image_count=image_count,
                extracted_text=extracted_text,
                error_message=error_message,
                images=image_rows,
                match_reason=f"{entry.reason}（来自 i学习 实验报告作答）",
            ),
            what=f"写实验报告提交 student_id={student.id}",
        )
        summary["matched"] += 1

    return summary


def _save_student_submission(
    *,
    report_id: int,
    rubric_id: int,
    class_id: int | None,
    student_id: int,
    source_filename: str,
    source_key: str,
    size_bytes: int,
    checksum: str,
    content_checksum: str,
    image_count: int,
    extracted_text: str | None,
    error_message: str | None,
    images: list[tuple[int, str, str, str, int, str]],
    match_reason: str,
) -> None:
    """一个学生 = 一个短事务。写完立刻提交，尽快把 SQLite 的写锁放掉。

    之前是十几个人共用一个事务，中间还要渲染 PDF，一个事务能占几十秒 ——
    几个班同时抓就会互相 `database is locked`。

    ``images`` 是已经落到对象存储里的图片信息
    （seq, 文件名, object_key, content_type, 字节数, 校验和）。
    """
    db = SessionLocal()
    try:
        student = db.get(Student, student_id)
        if student is None:
            return
        submission = (
            db.execute(
                select(IstudyLabSubmission).where(
                    IstudyLabSubmission.report_id == report_id,
                    IstudyLabSubmission.student_id == student_id,
                )
            )
            .scalars()
            .first()
        )
        if submission is None:
            submission = IstudyLabSubmission(report_id=report_id, student_id=student_id)
            db.add(submission)
            db.flush()

        submission.state = SubmissionState.submitted
        submission.file_state = FileState.ready
        submission.source_filename = source_filename
        submission.object_key = source_key
        submission.content_type = guess_content_type(source_filename)
        submission.size_bytes = size_bytes
        submission.checksum = checksum
        submission.content_checksum = content_checksum
        submission.image_count = image_count
        submission.extracted_text = extracted_text
        submission.error_message = error_message

        for old in list(submission.files):
            db.delete(old)
        db.flush()

        for seq, filename, object_key, content_type, size_bytes_img, checksum_img in images:
            db.add(
                IstudyLabFile(
                    submission_id=submission.id,
                    seq=seq,
                    role="answer",
                    filename=filename,
                    object_key=object_key,
                    content_type=content_type,
                    size_bytes=size_bytes_img,
                    checksum=checksum_img,
                )
            )

        rubric = db.get(Rubric, rubric_id)
        if rubric is not None:
            _upsert_grading_result(
                db,
                rubric=rubric,
                student=student,
                class_id=class_id,
                name=source_filename,
                size_bytes=size_bytes,
                checksum=content_checksum,
                object_key=source_key,
                match_reason=match_reason,
            )
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="istudy-lab")
    return _executor


# 一次抓取超过这么久还没结束，就当它已经死了（后端被重启、线程挂了之类），
# 允许重新抓，不然这条报告会永远卡在「正在下载」。
EXPORT_STALE_SECONDS = 20 * 60


def is_export_running(report: IstudyLabReport, *, now: datetime | None = None) -> bool:
    if report.sync_state != LabSyncState.running:
        return False
    if report.sync_started_at is None:
        return False
    now = now or datetime.now()
    return (now - report.sync_started_at).total_seconds() < EXPORT_STALE_SECONDS


def _mark_report_state(
    report_id: int,
    *,
    state: LabSyncState,
    message: str,
    started_at: datetime | None = None,
    finished: bool = False,
) -> None:
    """独立短事务更新抓取进度。抢锁就退避重试，别让「状态」本身写不进去。"""

    def action() -> None:
        db = SessionLocal()
        try:
            report = db.get(IstudyLabReport, report_id)
            if report is None:
                return
            report.sync_state = state
            report.sync_message = message
            if started_at is not None:
                report.sync_started_at = started_at
            if finished:
                report.sync_finished_at = datetime.now()
            db.commit()
        finally:
            db.close()

    with_retry(action, what=f"更新实验报告抓取状态 report_id={report_id}")


async def _download_report_zip(
    cid: str, istudy_report_id: str, dept_id: str, fill_ids: list[str]
) -> bytes:
    async with IstudyClient() as client:
        return await client.lab_export_fill_pdf(
            istudy_report_id, fill_ids, dept_id=dept_id or "1860"
        )


def run_report_export(report_id: int, fill_ids: list[str] | None = None) -> None:
    """后台线程：下载 → 解包 → 抽作答图片 → 匹配学生 → 入库。

    全程用「短会话」：读参数 → 关会话 → 下载（可能几十秒）→ 再开短会话写库。
    之前一个会话从头攥到尾，下载那几十秒里连接还开着，几个班一起抓就互相锁死。
    """
    db = SessionLocal()
    try:
        report = db.get(IstudyLabReport, report_id)
        if report is None:
            logger.warning("实验报告 %s 不存在", report_id)
            return
        targets = _targets(report, student_ids=fill_ids)
        if not targets:
            db.close()
            _mark_report_state(
                report_id,
                state=LabSyncState.done,
                message="没有需要抓的作答",
                finished=True,
            )
            return
        ids = [str(item.istudy_fill_id) for item in targets if item.istudy_fill_id]
        cid = report.course.istudy_cid if report.course else ""
        istudy_report_id = report.istudy_report_id
        dept_id = report.istudy_dept_id or "1860"
        name = report.name
    finally:
        db.close()

    if not cid:
        _mark_report_state(
            report_id,
            state=LabSyncState.failed,
            message="这门课还没绑定 i学习 课程，请先同步一次实验报告列表",
            finished=True,
        )
        return

    _mark_report_state(
        report_id,
        state=LabSyncState.running,
        message=f"正在从 i学习 下载 {len(ids)} 名学生的作答",
        started_at=datetime.now(),
    )
    try:
        data = asyncio.run(_download_report_zip(cid, istudy_report_id, dept_id, ids))
        _mark_report_state(
            report_id,
            state=LabSyncState.running,
            message=f"已下载 {len(data) / 1024 / 1024:.1f} MB，正在解包抽图",
        )
        summary = persist_report_bundle(report_id, data, zip_name=f"{name}.zip")
        message = f"抓到 {summary['matched']} 名学生的作答（共 {summary['images']} 张图）"
        _mark_report_state(
            report_id, state=LabSyncState.done, message=message, finished=True
        )
        logger.info("实验报告抓取完成 report_id=%s %s", report_id, message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("抓实验报告作答失败 report_id=%s", report_id)
        try:
            _mark_report_state(
                report_id,
                state=LabSyncState.failed,
                message=str(exc),
                finished=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("记录实验报告抓取失败状态时又出错了 report_id=%s", report_id)


def reset_stale_sync_states() -> int:
    """启动时收尾：把上次没跑完、卡在「正在下载」的报告标成失败，允许重新抓。"""
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(IstudyLabReport).where(IstudyLabReport.sync_state == LabSyncState.running)
            )
            .scalars()
            .all()
        )
        for report in rows:
            report.sync_state = LabSyncState.failed
            report.sync_message = "上次抓取被中断了，可以重新抓一次"
            report.sync_finished_at = datetime.now()
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


def _run_report_export_safe(report_id: int, fill_ids: list[str] | None = None) -> None:
    try:
        run_report_export(report_id, fill_ids)
    except Exception:  # noqa: BLE001
        logger.exception("后台抓取实验报告任务异常 report_id=%s", report_id)


def enqueue_report_export_job(report_id: int, fill_ids: list[str] | None = None) -> None:
    _get_executor().submit(_run_report_export_safe, report_id, fill_ids)


def shutdown_report_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=False)
        _executor = None


# --------------------------------------------------------------------------- #
# 对比 / 名单
# --------------------------------------------------------------------------- #
async def compare_report_students(db: Session, report: IstudyLabReport) -> dict[str, Any]:
    """拿 i学习 上的「已交名单」和本地已抓的对比，找出还没抓的人。"""
    course = report.course
    if course is None or not course.istudy_cid:
        raise IstudyError("这门课还没绑定 i学习 课程，请先同步一次实验报告列表")

    async with IstudyClient() as client:
        istudy_rows = await client.lab_corrections(
            report.istudy_report_id, dept_id=report.istudy_dept_id or "1860"
        )

    students: dict[int, Student] = {}
    submissions: dict[int, IstudyLabSubmission] = {}
    if report.class_id:
        students = {
            item.id: item
            for item in db.execute(
                select(Student).where(Student.class_id == report.class_id)
            )
            .scalars()
            .all()
        }
        submissions = {
            item.student_id: item
            for item in db.execute(
                select(IstudyLabSubmission).where(IstudyLabSubmission.report_id == report.id)
            )
            .scalars()
            .all()
        }
    by_student_no = {item.student_no: item for item in students.values()}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in istudy_rows:
        student_no = _clean(item.get("belongNo"))
        if not student_no:
            continue
        seen.add(student_no)
        student = by_student_no.get(student_no)
        submission = submissions.get(student.id) if student else None
        fetched = bool(submission and submission.file_state == FileState.ready)
        if fetched:
            state = "fetched"
        elif student is None:
            state = "not_in_roster"
        else:
            state = "new"
        rows.append(
            {
                "student_no": student_no,
                "name": _clean(item.get("belongUname")) or (student.name if student else ""),
                # 「按人抓取」传的是 i学习 的 fillId
                "istudy_user_id": _clean(item.get("id")) or None,
                "answer_id": _clean(item.get("id")) or None,
                "submitted_at": _clean(item.get("submitTime")),
                "state": state,
                "fetched": fetched,
                "in_roster": student is not None,
                "image_count": submission.image_count if submission else 0,
            }
        )

    for student in students.values():
        if student.student_no in seen:
            continue
        submission = submissions.get(student.id)
        rows.append(
            {
                "student_no": student.student_no,
                "name": student.name,
                "istudy_user_id": None,
                "answer_id": None,
                "submitted_at": None,
                "state": "not_submitted",
                "fetched": bool(submission and submission.file_state == FileState.ready),
                "in_roster": True,
                "image_count": submission.image_count if submission else 0,
            }
        )

    order = {"new": 0, "not_in_roster": 1, "fetched": 2, "not_submitted": 3}
    rows.sort(key=lambda item: (order.get(item["state"], 9), item["student_no"]))

    return {
        "report_id": report.id,
        "report_name": report.name,
        "class_id": report.class_id,
        "class_name": report.class_.name if report.class_ else "",
        "istudy_submitted": len(istudy_rows),
        "local_fetched": sum(1 for item in rows if item["fetched"]),
        "new_count": sum(1 for item in rows if item["state"] == "new"),
        "students": rows,
    }


def switch_report_rubric(
    db: Session, report: IstudyLabReport, rubric: Rubric
) -> dict[str, Any]:
    """把这次实验换成用另一份评分细则来评（结果跟着改挂过去）。"""
    reports = report_group(db, report)
    moved = skipped = 0
    current_ids = {
        item.rubric_id for item in reports if item.rubric_id and item.rubric_id != rubric.id
    }
    taken = {
        row.student_id
        for row in db.execute(
            select(GradingResult).where(GradingResult.rubric_id == rubric.id)
        )
        .scalars()
        .all()
    }
    for old_id in current_ids:
        for row in (
            db.execute(select(GradingResult).where(GradingResult.rubric_id == old_id))
            .scalars()
            .all()
        ):
            if row.student_id in taken:
                db.delete(row)
                skipped += 1
                continue
            row.rubric_id = rubric.id
            taken.add(row.student_id)
            moved += 1
    for item in reports:
        item.rubric_id = rubric.id
    db.flush()
    return {"moved": moved, "skipped": skipped, "rubric_id": rubric.id}
