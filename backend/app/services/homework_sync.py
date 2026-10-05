"""i学习 作业：同步作业列表 + 抓附件入库。

抓取（异步，不碰数据库）
    scrape_course_works  我教的课 → 课程 → 作业列表，按班级展开成一条条作业

落库
    apply_works          写进 istudy_works，并按作业自动建评分细则

抓附件（i学习 的导出是异步的，整条链路要跑几分钟）
    enqueue_export       在请求里建一条导出任务，立刻返回给前端轮询
    run_export_job       后台线程：提交导出 → 轮询下载中心 → 下载 → 解包 →
                         抽作答图片 → 匹配学生 → 写 istudy_submissions / grading_results
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Class,
    Course,
    CourseRubric,
    ExportStatus,
    FileState,
    GradingResult,
    IstudyExport,
    IstudySubmission,
    IstudySubmissionFile,
    IstudyWork,
    ResultStatus,
    Rubric,
    Student,
    SubmissionState,
    WorkStatus,
)
from ..storage import get_storage
from ..utils import safe_filename, sha256_hex
from . import archive
from .answer_files import AnswerFileError, extract_answer_files
from .istudy import IstudyClient, IstudyError

logger = logging.getLogger(__name__)

SCRAPE_CONCURRENCY = 4
EXPORT_POLL_SECONDS = 5
EXPORT_TIMEOUT_SECONDS = 30 * 60

DEFAULT_CRITERIA = (
    "1. 作答完整性（40 分）：题目要求的每个问题都有对应解答，没有整题遗漏。\n"
    "2. 过程与依据（30 分）：有必要的公式、推导、图示或计算过程，依据正确、步骤连贯。\n"
    "3. 结果正确性（20 分）：最终结论与数值正确，单位、有效数字、符号规范。\n"
    "4. 书写与规范（10 分）：字迹清晰可辨，图表标注完整，卷面整洁。\n"
)

# 自动建的细则不写「任务说明」：这段文字会作为【任务说明】原样发给模型，
# 写「这里是占位内容」对模型是纯噪音，不如留空。
DEFAULT_DESCRIPTION: str | None = None


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
@dataclass
class ScrapedWork:
    """一条「作业 × 班级」。"""

    istudy_task_id: str
    istudy_library_id: str
    istudy_work_id: str
    istudy_clazzid: str
    class_name: str
    name: str
    time_text: str = ""
    status: WorkStatus = WorkStatus.ongoing
    submitted_count: int = 0
    unsubmitted_count: int = 0
    pending_count: int = 0
    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass
class ScrapeResult:
    cid: str
    cpi: str
    course_name: str = ""
    works: list[ScrapedWork] = field(default_factory=list)
    class_map: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


_TIME_RE = re.compile(
    r"作答时间[:：]\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*至\s*"
    r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})"
)


def parse_time_text(text: str, *, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """把「作答时间：09-08 12:00至 09-11 23:59」解析成 (开始, 结束)。

    列表页只给月日不给年，按「离当前时间最近」补年份。
    """
    match = _TIME_RE.search(text or "")
    if not match:
        return None, None
    now = now or datetime.now()
    try:
        parts = [int(value) for value in match.groups()]
    except ValueError:
        return None, None

    def build(month: int, day: int, hour: int, minute: int) -> datetime | None:
        best: datetime | None = None
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                candidate = datetime(year, month, day, hour, minute)
            except ValueError:
                continue
            if best is None or abs((candidate - now).total_seconds()) < abs((best - now).total_seconds()):
                best = candidate
        return best

    start = build(parts[0], parts[1], parts[2], parts[3])
    end = build(parts[4], parts[5], parts[6], parts[7])
    if start and end and end < start:
        end = end.replace(year=end.year + 1)
    return start, end


async def scrape_course_works(
    cid: str, cpi: str, *, course_name: str = "", scraped_at: datetime | None = None
) -> ScrapeResult:
    """抓一门课的作业列表，按班级展开。

    要点：按班级筛选时列表里的 id 是 workId，课程级（不筛班级）时的 id 才是 taskId，
    而导出接口要的是 taskId，所以每个 workId 都要去批阅页换一次。
    """
    result = ScrapeResult(cid=cid, cpi=cpi, course_name=course_name)
    now = scraped_at or datetime.now()
    limiter = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async def guarded(coro):
        async with limiter:
            return await coro

    async with IstudyClient() as client:
        context = await client.course_context(cid, cpi)
        try:
            classes = await client.list_classes(cid, context["clazzid"], cpi)
        except IstudyError as exc:
            result.notes.append(f"班级列表没抓到：{exc}")
            return result

        classes = [item for item in classes if item["name"] and item["name"] != "默认班级"]
        for item in classes:
            result.class_map[item["clazz_id"]] = item["name"]

        per_class_raw = await asyncio.gather(
            *[
                guarded(
                    client.list_works(cid, cpi, context, select_classid=item["clazz_id"], status="-1")
                )
                for item in classes
            ],
            return_exceptions=True,
        )

        status_raw = await asyncio.gather(
            *[
                guarded(client.list_works(cid, cpi, context, select_classid="0", status=code))
                for code in ("0", "1", "2")
            ],
            return_exceptions=True,
        )
        status_by_task: dict[str, WorkStatus] = {}
        for code, rows in zip(("not_started", "ongoing", "ended"), status_raw, strict=True):
            if isinstance(rows, BaseException):
                result.notes.append(f"状态 {code} 的作业没抓到：{rows}")
                continue
            for row in rows:
                status_by_task[row["istudy_id"]] = WorkStatus(code)

        entries: list[dict[str, Any]] = []
        for item, rows in zip(classes, per_class_raw, strict=True):
            if isinstance(rows, BaseException):
                result.notes.append(f"班级「{item['name']}」的作业没抓到：{rows}")
                continue
            for row in rows:
                entries.append({**row, "clazzid": item["clazz_id"], "class_name": item["name"]})

        task_cache: dict[tuple[str, str], str] = {}

        async def resolve_task_id(entry: dict[str, Any]) -> str:
            key = (entry["clazzid"], entry["istudy_id"])
            if key in task_cache:
                return task_cache[key]
            try:
                mark = await guarded(
                    client.work_mark_context(cid, cpi, entry["clazzid"], entry["istudy_id"])
                )
            except IstudyError as exc:
                logger.warning("批阅页取 taskId 失败（%s）：%s", entry["istudy_id"], exc)
                return ""
            task_cache[key] = mark["task_id"]
            for option in mark["class_options"]:
                result.class_map.setdefault(option["clazzid"], option["name"])
            return mark["task_id"]

        task_ids = await asyncio.gather(*[resolve_task_id(entry) for entry in entries])

    for entry, task_id in zip(entries, task_ids, strict=True):
        if not task_id:
            continue
        start, end = parse_time_text(entry["time_text"], now=now)
        result.works.append(
            ScrapedWork(
                istudy_task_id=task_id,
                istudy_library_id=entry.get("library_id") or "",
                istudy_work_id=entry["istudy_id"],
                istudy_clazzid=entry["clazzid"],
                class_name=entry["class_name"],
                name=entry["name"],
                time_text=entry["time_text"],
                status=status_by_task.get(task_id, WorkStatus.ongoing),
                submitted_count=entry["submitted_count"],
                unsubmitted_count=entry["unsubmitted_count"],
                pending_count=entry["pending_count"],
                start_at=start,
                end_at=end,
            )
        )

    seen: set[str] = set()
    unique: list[ScrapedWork] = []
    for work in result.works:
        if work.istudy_work_id in seen:
            continue
        seen.add(work.istudy_work_id)
        unique.append(work)
    result.works = unique
    return result


# --------------------------------------------------------------------------- #
# 落库：作业列表 + 评分细则
# --------------------------------------------------------------------------- #
def local_class_id(db: Session, course: Course, class_name: str) -> int | None:
    """按班级名把本地班级找出来（本地班级挂在课程下）。"""
    from ..models import CourseClass

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


def work_folder(work: IstudyWork) -> str:
    """对象存储里这次作业的文件夹名。

    用「班级-作业名」而不是纯数字 id，例如 ``25人工智能本1-第1周作业``，
    这样直接翻磁盘也看得懂。班级名和作业名里的非法字符会被 safe_filename 换掉。
    """
    class_name = work.class_.name if work.class_ else ""
    label = f"{class_name}-{work.name}" if class_name else work.name
    return safe_filename(label, fallback=f"work-{work.id}")


def work_prefix(work: IstudyWork) -> str:
    return f"istudy/works/{work_folder(work)}"


def _absorb_rubric(db: Session, keep: Rubric, dup: Rubric) -> None:
    """把重复的细则并进 keep：作业指向和评分结果都改过去，然后删掉 dup。"""
    keep_course_ids = {link.course_id for link in keep.course_links}
    for link in dup.course_links:
        if link.course_id not in keep_course_ids:
            db.add(CourseRubric(course_id=link.course_id, rubric_id=keep.id))

    for work in (
        db.execute(select(IstudyWork).where(IstudyWork.rubric_id == dup.id)).scalars().all()
    ):
        work.rubric_id = keep.id

    taken = {
        row.student_id
        for row in db.execute(
            select(GradingResult).where(GradingResult.rubric_id == keep.id)
        )
        .scalars()
        .all()
    }
    for row in (
        db.execute(select(GradingResult).where(GradingResult.rubric_id == dup.id))
        .scalars()
        .all()
    ):
        if row.student_id in taken:
            db.delete(row)
            continue
        row.rubric_id = keep.id
        taken.add(row.student_id)

    db.flush()
    db.delete(dup)
    db.flush()


def ensure_rubric(
    db: Session,
    course: Course,
    *,
    library_id: str | None,
    task_id: str | None,
    name: str,
) -> Rubric:
    """一次 i学习 作业对应一份评分细则。

    注意别按 taskId 建细则：一次作业发给几个班就会有几个批次（taskId），
    但它们的 workLibraryId 是一样的。所以按 ``istudy_library_id`` 认，重复的并掉。
    """
    from ..models import GradingKind

    rubric: Rubric | None = None
    if library_id:
        rubric = (
            db.execute(select(Rubric).where(Rubric.istudy_library_id == library_id))
            .scalars()
            .first()
        )
    if rubric is None and task_id:
        # 老数据（还没记 library_id 的）按批次 id 兜底认领
        rubric = (
            db.execute(
                select(Rubric).where(
                    Rubric.istudy_library_id.is_(None), Rubric.istudy_task_id == task_id
                )
            )
            .scalars()
            .first()
        )

    if rubric is None:
        rubric = Rubric(
            name=name,
            kind=GradingKind.homework,
            description=DEFAULT_DESCRIPTION,
            criteria=DEFAULT_CRITERIA,
            total_score=100.0,
            source="istudy",
            istudy_task_id=task_id or None,
            istudy_library_id=library_id or None,
        )
        db.add(rubric)
        db.flush()
    else:
        if library_id and not rubric.istudy_library_id:
            rubric.istudy_library_id = library_id
        if task_id and not rubric.istudy_task_id:
            rubric.istudy_task_id = task_id
        db.flush()

    # 把「其实是一次作业」的重复细则并掉：
    #   1) 已经认领了同一个 workLibraryId 的；
    #   2) 同一个批次 id、但还没认领 library_id 的老记录。
    if library_id:
        duplicates = (
            db.execute(
                select(Rubric).where(
                    Rubric.id != rubric.id,
                    or_(
                        Rubric.istudy_library_id == library_id,
                        and_(
                            Rubric.istudy_library_id.is_(None),
                            Rubric.istudy_task_id == task_id,
                        ),
                    ),
                )
            )
            .scalars()
            .all()
        )
        for dup in duplicates:
            logger.info("合并重复的评分细则：%s(#%s) -> #%s", dup.name, dup.id, rubric.id)
            _absorb_rubric(db, rubric, dup)

    linked = db.execute(
        select(CourseRubric.id).where(
            CourseRubric.course_id == course.id, CourseRubric.rubric_id == rubric.id
        )
    ).scalars().first()
    if linked is None:
        db.add(CourseRubric(course_id=course.id, rubric_id=rubric.id))
        db.flush()
    return rubric


def list_work_groups(
    db: Session,
    course_id: int,
    *,
    class_id: int | None = None,
    status: WorkStatus | None = None,
) -> list[dict[str, Any]]:
    """按「作业名」聚合成列表页要显示的一行行。

    一次作业发给几个班就会有几个发布批次（taskId），但它们的 workLibraryId 相同，
    所以按 ``istudy_library_id`` 归并 —— 一行就是「一次作业」，班级明细挂在 classes 里。
    """
    from sqlalchemy import case, func

    stmt = select(IstudyWork).where(IstudyWork.course_id == course_id)
    if class_id is not None:
        stmt = stmt.where(IstudyWork.class_id == class_id)
    if status is not None:
        stmt = stmt.where(IstudyWork.status == status)
    works = list(db.execute(stmt.order_by(IstudyWork.id)).scalars().all())
    if not works:
        return []

    work_ids = [item.id for item in works]

    submission_stats = {
        row[0]: row[1:]
        for row in db.execute(
            select(
                IstudySubmission.work_id,
                func.sum(case((IstudySubmission.file_state == FileState.ready, 1), else_=0)),
                func.sum(case((IstudySubmission.state == SubmissionState.missing, 1), else_=0)),
                func.sum(
                    case(
                        (IstudySubmission.file_state == FileState.ready, IstudySubmission.image_count),
                        else_=0,
                    )
                ),
            )
            .where(IstudySubmission.work_id.in_(work_ids))
            .group_by(IstudySubmission.work_id)
        )
    }

    # 评分结果按「细则 × 班级」统计：一份细则会被同一作业下的多个班共用，
    # 按班级分开数，既不会漏也不会因为多个班级行而重复计算。
    rubric_ids = {item.rubric_id for item in works if item.rubric_id}
    result_stats: dict[tuple[int, int | None], tuple[int, int]] = {}
    result_by_rubric: dict[int, tuple[int, int]] = {}
    if rubric_ids:
        for rubric_id, class_id, total, graded in db.execute(
            select(
                GradingResult.rubric_id,
                GradingResult.class_id,
                func.count(GradingResult.id),
                func.sum(case((GradingResult.status == ResultStatus.graded, 1), else_=0)),
            )
            .where(GradingResult.rubric_id.in_(rubric_ids))
            .group_by(GradingResult.rubric_id, GradingResult.class_id)
        ):
            result_stats[(rubric_id, class_id)] = (total or 0, graded or 0)
            previous = result_by_rubric.get(rubric_id, (0, 0))
            result_by_rubric[rubric_id] = (
                previous[0] + (total or 0),
                previous[1] + int(graded or 0),
            )

    latest_export: dict[int, IstudyExport] = {}
    for row in (
        db.execute(
            select(IstudyExport)
            .where(IstudyExport.work_id.in_(work_ids))
            .order_by(IstudyExport.id)
        )
        .scalars()
        .all()
    ):
        latest_export[row.work_id] = row

    groups: dict[str, dict[str, Any]] = {}
    for work in works:
        captured, missing, images = submission_stats.get(work.id, (0, 0, 0))
        if work.class_id is None:
            # 本地还没建这个班：退回到按细则统计
            results, graded = result_by_rubric.get(work.rubric_id or -1, (0, 0))
        else:
            results, graded = result_stats.get((work.rubric_id or -1, work.class_id), (0, 0))
        class_row = {
            "work_id": work.id,
            "class_id": work.class_id,
            "class_name": work.class_.name if work.class_ else "",
            "rubric_id": work.rubric_id,
            "istudy_clazzid": work.istudy_clazzid,
            "istudy_work_id": work.istudy_work_id,
            "status": work.status,
            "start_at": work.start_at,
            "end_at": work.end_at,
            "submitted_count": work.submitted_count,
            "unsubmitted_count": work.unsubmitted_count,
            "pending_count": work.pending_count,
            "captured_count": int(captured or 0),
            "missing_count": int(missing or 0),
            "image_count": int(images or 0),
            "result_count": results,
            "graded_count": int(graded or 0),
            "last_export": latest_export.get(work.id),
        }

        group_key = work.istudy_library_id or work.name
        group = groups.get(group_key)
        if group is None:
            group = {
                "name": work.name,
                "library_id": work.istudy_library_id,
                "task_ids": [],
                "rubric_ids": [],
                "status_breakdown": {},
                "start_at": work.start_at,
                "end_at": work.end_at,
                "rubric_id": work.rubric_id,
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
            groups[group_key] = group

        if work.istudy_task_id not in group["task_ids"]:
            group["task_ids"].append(work.istudy_task_id)
        if work.rubric_id and work.rubric_id not in group["rubric_ids"]:
            group["rubric_ids"].append(work.rubric_id)
        key = work.status.value
        group["status_breakdown"][key] = group["status_breakdown"].get(key, 0) + 1
        if work.start_at and (group["start_at"] is None or work.start_at < group["start_at"]):
            group["start_at"] = work.start_at
        if work.end_at and (group["end_at"] is None or work.end_at > group["end_at"]):
            group["end_at"] = work.end_at
        group["submitted_count"] += work.submitted_count
        group["unsubmitted_count"] += work.unsubmitted_count
        group["pending_count"] += work.pending_count
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
        group["status"] = (
            next(iter(breakdown)) if len(breakdown) == 1 else None
        )
        group["status"] = WorkStatus(group["status"]) if group["status"] else None
        group["classes"].sort(key=lambda item: (item["class_name"] or ""))
        items.append(group)

    items.sort(
        key=lambda item: (item["end_at"] or item["start_at"] or datetime.min),
        reverse=True,
    )
    return items


def apply_works(db: Session, course: Course, scrape: ScrapeResult) -> dict[str, int]:
    """把抓到的作业写进 istudy_works（按 istudy_work_id 幂等更新）。"""
    created = updated = 0
    now = datetime.now()
    if scrape.cid and course.istudy_cid != scrape.cid:
        course.istudy_cid = scrape.cid
    if scrape.cpi and course.istudy_cpi != scrape.cpi:
        course.istudy_cpi = scrape.cpi

    for item in scrape.works:
        rubric = ensure_rubric(
            db,
            course,
            library_id=item.istudy_library_id or None,
            task_id=item.istudy_task_id,
            name=item.name,
        )
        row = (
            db.execute(
                select(IstudyWork).where(IstudyWork.istudy_work_id == item.istudy_work_id)
            )
            .scalars()
            .first()
        )
        class_id = local_class_id(db, course, item.class_name)
        payload = {
            "course_id": course.id,
            "class_id": class_id,
            "rubric_id": rubric.id,
            "istudy_cid": scrape.cid,
            "istudy_clazzid": item.istudy_clazzid,
            "istudy_task_id": item.istudy_task_id,
            "istudy_library_id": item.istudy_library_id or None,
            "name": item.name,
            "status": item.status,
            "start_at": item.start_at,
            "end_at": item.end_at,
            "time_text": item.time_text,
            "submitted_count": item.submitted_count,
            "unsubmitted_count": item.unsubmitted_count,
            "pending_count": item.pending_count,
            "last_synced_at": now,
        }
        if row is None:
            db.add(IstudyWork(istudy_work_id=item.istudy_work_id, **payload))
            created += 1
            continue
        for key, value in payload.items():
            setattr(row, key, value)
        updated += 1

    db.flush()
    return {"created": created, "updated": updated}


# --------------------------------------------------------------------------- #
# 抓附件：导出 → 下载 → 解包 → 抽作答图片 → 匹配学生
# --------------------------------------------------------------------------- #

def assignment_works(db: Session, work: IstudyWork) -> list[IstudyWork]:
    """「这次作业」包括的所有班级行（同一个 workLibraryId 的批次）。"""
    if not work.istudy_library_id:
        return [work]
    rows = (
        db.execute(
            select(IstudyWork).where(
                IstudyWork.istudy_library_id == work.istudy_library_id
            )
        )
        .scalars()
        .all()
    )
    return list(rows) or [work]


async def compare_work_students(db: Session, work: IstudyWork) -> dict[str, Any]:
    """拿 i学习 上的「已交名单」和本地已抓的对比，找出还没抓的人。

    用途：老师只想知道「谁又交了、还没拉下来」，按人精准导出，不用整个班重抓一遍。
    """
    course = work.course
    if course is None or not (course.istudy_cid and course.istudy_cpi):
        raise IstudyError("这门课还没绑定 i学习 课程，请先同步一次作业列表")

    async with IstudyClient() as client:
        istudy_rows = await client.list_submitted_students(
            course.istudy_cid,
            course.istudy_cpi,
            work.istudy_clazzid,
            work.istudy_work_id,
        )

    students: dict[int, Student] = {}
    submissions: dict[int, IstudySubmission] = {}
    if work.class_id:
        students = {
            item.id: item
            for item in db.execute(
                select(Student).where(Student.class_id == work.class_id)
            )
            .scalars()
            .all()
        }
        submissions = {
            item.student_id: item
            for item in db.execute(
                select(IstudySubmission).where(IstudySubmission.work_id == work.id)
            )
            .scalars()
            .all()
        }
    by_student_no = {item.student_no: item for item in students.values()}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in istudy_rows:
        student_no = item["student_no"]
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
                "name": item["name"] or (student.name if student else ""),
                "istudy_user_id": item["istudy_user_id"],
                "answer_id": item["answer_id"],
                "submitted_at": item["submitted_at"],
                "state": state,
                "fetched": fetched,
                "in_roster": student is not None,
                "image_count": submission.image_count if submission else 0,
            }
        )

    # 本地名单里有、但 i学习 那边没交的（导不出来，只能标一下）
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
        "work_id": work.id,
        "work_name": work.name,
        "class_id": work.class_id,
        "class_name": work.class_.name if work.class_ else "",
        "istudy_submitted": len(istudy_rows),
        "local_fetched": sum(1 for item in rows if item["fetched"]),
        "new_count": sum(1 for item in rows if item["state"] == "new"),
        "students": rows,
    }


def switch_assignment_rubric(db: Session, work: IstudyWork, rubric: Rubric) -> dict[str, Any]:
    """把「这次作业」换成用另一份评分细则来评。

    评分细则本身只是一份丢给 AI 看的标准，跟作业没有强绑定关系，所以允许在这里换。
    换了以后这次作业原来的评分结果会跟着挂到新细则下（同一个学生已经在新细则下有了
    结果就丢掉旧的），作业行上的 rubric_id 也一起更新。
    """
    works = assignment_works(db, work)
    library_ids = {item.istudy_library_id for item in works if item.istudy_library_id}

    if library_ids:
        other = (
            db.execute(
                select(IstudyWork).where(
                    IstudyWork.rubric_id == rubric.id,
                    or_(
                        IstudyWork.istudy_library_id.is_(None),
                        IstudyWork.istudy_library_id.notin_(library_ids),
                    ),
                )
            )
            .scalars()
            .first()
        )
        if other is not None:
            raise ValueError(
                f"细则「{rubric.name}」已经在给别的作业（{other.name}）用了，"
                "一份细则不能同时挂在两次作业下。可以先去「评分细则」页复制一份。"
            )

    current_ids = {item.rubric_id for item in works if item.rubric_id and item.rubric_id != rubric.id}
    moved = skipped = 0
    for old_id in current_ids:
        taken = {
            row.student_id
            for row in db.execute(
                select(GradingResult).where(GradingResult.rubric_id == rubric.id)
            )
            .scalars()
            .all()
        }
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

    for item in works:
        item.rubric_id = rubric.id
    db.flush()
    return {"moved": moved, "skipped": skipped, "rubric_id": rubric.id}

def enqueue_export(
    db: Session,
    work: IstudyWork,
    *,
    content: int = 0,
    fmt: int = 1,
    person_ids: list[str] | None = None,
) -> IstudyExport:
    """建一条导出任务（真正干活在后台线程里）。

    person_ids 传了就只导出这几个人（i学习 的用户 id）。
    """
    if person_ids:
        # 实测：i学习 的「按人导出」配 PDF 会一直卡在「导出中」不产出文件，
        # Word 和「仅提交附件」都正常。所以按人抓取统一走「仅提交附件」——
        # 拿到的就是学生自己交的原图/原文件，正好是 AI 评审要的东西。
        content, fmt = 1, 0
    row = IstudyExport(
        work_id=work.id,
        content=content,
        fmt=fmt,
        person_ids=",".join(person_ids) if person_ids else None,
        status=ExportStatus.queued,
        message="已提交，等待 i学习 打包",
    )
    db.add(row)
    db.flush()
    return row


async def run_export_remote(
    cid: str, cpi: str, work: IstudyWork, export: IstudyExport
) -> tuple[str, bytes]:
    """提交 i学习 导出并等打包完成，返回 (下载中心条目 id, 压缩包字节)。

    两个坑：
      1. 同一个作业 + 班级 + 格式重新导出时，i学习 会复用下载中心里那条老记录，
         把它重新置成「导出中」，所以不能只认「新出现的 id」；
      2. 下载中心是老师共用的，别人手动导出的东西也会出现在里面，所以要按
         作业名 + 班级名 + 格式后缀挑自己那条。
    """
    class_name = work.class_.name if work.class_ else ""
    suffix = _expected_export_suffix(export.content, export.fmt)
    by_person = bool(export.person_ids)

    def matches(item: dict[str, str]) -> bool:
        if work.name not in item["name"]:
            return False
        if class_name and class_name not in item["name"]:
            return False
        # 「按人导出」的包名字里会多一个 (按人导出)，别跟整班导出的包搞混
        if by_person != ("按人导出" in item["name"]):
            return False
        return not suffix or suffix in item["name"]

    async with IstudyClient() as client:
        before_done = {
            item["id"]
            for item in await client.list_download_center(cid, cpi)
            if item["done"] == "1"
        }
        payload = await client.request_work_export(
            cid,
            cpi,
            clazzid=work.istudy_clazzid,
            work_id=work.istudy_work_id,
            task_id=work.istudy_task_id,
            content=export.content,
            fmt=export.fmt,
            scope=export.pack_scope,
            person_ids=[item for item in (export.person_ids or "").split(",") if item],
        )
        if str(payload.get("status")) not in ("0", "1"):
            raise IstudyError(f"i学习 拒绝导出：{payload}")

        deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
        polls = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(EXPORT_POLL_SECONDS)
            polls += 1
            items = await client.list_download_center(cid, cpi)
            usable = [item for item in items if item["done"] == "1" and item["url"]]

            # 优先用这次新出现的条目
            fresh = [item for item in usable if item["id"] not in before_done and matches(item)]
            picked = fresh[0] if fresh else None

            # 复用老条目时，平台会先把它置成「导出中」，等几轮再认，
            # 免得刚提交就抓到上一次的旧包
            if picked is None and polls >= 3:
                reused = [item for item in usable if matches(item)]
                picked = reused[0] if reused else None

            if picked is None:
                continue
            return picked["id"], await client.download_export(picked["url"])
        raise IstudyError("等 i学习 打包超时（超过 30 分钟）")


def _expected_export_suffix(content: int, fmt: int) -> str:
    """下载中心里文件名的后缀，用来认出自己那条任务。"""
    if content == 1:
        return "(附件)"
    if content == 0:
        return "(pdf)" if fmt == 1 else "(word)"
    return ""


def _content_fingerprint(answer) -> str:  # noqa: ANN001
    """给「学生实际写了什么」算个指纹：抽出来的图片（按顺序）+ 正文文字。

    为什么不用文件本身的 sha256：实测 i学习 每次导出的 .doc 外层字节都会变
    （同一份作业两次导出差 8 个字节），但里面的图片和文字一模一样。
    用文件哈希会导致「没重新交也会被判定成换了文件」，把已经评过的分误清掉。
    """
    parts = [sha256_hex(image.data) for image in answer.images]
    return sha256_hex(("|".join(parts) + "\n" + (answer.text or "")).encode("utf-8"))


def _upsert_submission(db: Session, work: IstudyWork, student: Student) -> IstudySubmission:
    """取回（或新建）某个学生这次的提交记录。"""
    row = (
        db.execute(
            select(IstudySubmission).where(
                IstudySubmission.work_id == work.id, IstudySubmission.student_id == student.id
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = IstudySubmission(work_id=work.id, student_id=student.id)
        db.add(row)
        db.flush()
    return row


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
    """把学生的提交登记成一条待评审记录。

    重新抓取时：内容没变就保留原来的评审结果；内容换了（指纹不同）就退回待评审，
    免得 AI 分是照着旧文件给的、却挂在新文件上。
    """
    from ..utils import guess_content_type

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
        previous_checksum is None
        or checksum is None
        or previous_checksum != checksum
    )
    if file_changed or row.status != ResultStatus.graded:
        row.status = ResultStatus.pending
        row.score = None
        row.comment = None
        row.error_message = None
        row.model = None
        row.graded_at = None
        row.is_manual = False


def persist_export_bundle(
    db: Session, export: IstudyExport, data: bytes, *, zip_name: str | None = None
) -> dict[str, Any]:
    """把下载下来的压缩包落进对象存储，并拆成一个个学生的提交。"""
    from collections import Counter

    from ..utils import guess_content_type

    work = export.work
    course = work.course
    rubric = work.rubric or ensure_rubric(
        db,
        course,
        library_id=work.istudy_library_id,
        task_id=work.istudy_task_id,
        name=work.name,
    )
    work.rubric_id = rubric.id

    storage = get_storage()
    bundle_name = zip_name or f"{work.name}.zip"
    export_key = f"{work_prefix(work)}/exports/{export.id}-{safe_filename(bundle_name)}"
    storage.put_bytes(export_key, data, "application/zip")
    export.object_key = export_key
    export.size_bytes = len(data)

    students = (
        db.execute(select(Student).where(Student.class_id == work.class_id)).scalars().all()
        if work.class_id
        else []
    )
    student_by_id = {student.id: student for student in students}
    class_name = work.class_.name if work.class_ else ""

    summary: dict[str, Any] = {
        "matched": 0,
        "images": 0,
        "missing": 0,
        "issues": [],
    }
    if not students:
        summary["issues"].append("这个班在本地还没有学生名单，先导入名单再来抓作业")
        export.message = summary["issues"][0]
        return summary

    refs = [
        archive.StudentRef(
            id=student.id, student_no=student.student_no, name=student.name, class_name=class_name
        )
        for student in students
    ]

    scan = archive.scan_uploads([(bundle_name, data)])
    # 「教师批注.zip」里是老师批注过的版本，不当作学生作答
    leaves = [leaf for leaf in scan.leaves if "教师批注" not in leaf.parents]
    matched, issues = archive.assign_students(leaves, refs)
    summary["issues"] = [f"{item.filename}：{item.reason}" for item in issues[:20]]

    matched_ids: set[int] = set()
    for entry in matched:
        student = student_by_id.get(entry.student_id)
        if student is None:
            continue
        matched_ids.add(student.id)
        leaf = entry.leaf
        base = f"{work_prefix(work)}/students/{safe_filename(student.student_no)}"
        source_key = f"{base}/{safe_filename(leaf.name)}"
        # 重抓时先清掉这个学生上一版的文件，免得换了文件名 / 少了几页之后留下孤儿文件
        storage.delete_prefix(base)
        storage.put_bytes(source_key, leaf.data, guess_content_type(leaf.name))

        submission = _upsert_submission(db, work, student)
        submission.state = SubmissionState.submitted
        submission.file_state = FileState.ready
        submission.source_filename = leaf.name
        submission.object_key = source_key
        submission.content_type = guess_content_type(leaf.name)
        submission.size_bytes = len(leaf.data)
        submission.checksum = sha256_hex(leaf.data)
        submission.error_message = None

        for old in list(submission.files):
            db.delete(old)
        db.flush()

        try:
            answer = extract_answer_files(leaf.name, leaf.data)
        except AnswerFileError as exc:
            submission.image_count = 0
            submission.extracted_text = None
            submission.content_checksum = sha256_hex(leaf.data)
            submission.error_message = str(exc)
            summary["issues"].append(f"{student.name}：{exc}")
        else:
            for image in answer.images:
                image_key = f"{base}/answer/{image.seq:02d}-{safe_filename(image.filename)}"
                storage.put_bytes(image_key, image.data, image.content_type)
                db.add(
                    IstudySubmissionFile(
                        submission_id=submission.id,
                        seq=image.seq,
                        role="answer",
                        filename=image.filename,
                        object_key=image_key,
                        content_type=image.content_type,
                        size_bytes=len(image.data),
                        checksum=sha256_hex(image.data),
                    )
                )
            submission.image_count = len(answer.images)
            submission.extracted_text = answer.text
            submission.content_checksum = _content_fingerprint(answer)
            summary["images"] += len(answer.images)
            if answer.note:
                summary["issues"].append(f"{student.name}：{answer.note}")

        _upsert_grading_result(
            db,
            rubric=rubric,
            student=student,
            class_id=work.class_id,
            name=leaf.name,
            size_bytes=len(leaf.data),
            checksum=submission.content_checksum,
            object_key=source_key,
            match_reason=f"{entry.reason}（来自 i学习 作业附件）",
        )
        summary["matched"] += 1

        # 一个班可能有几百人，边导边提交，别让写事务开着太久——
        # 否则同时在跑 AI 评审的话会互相抢锁（database is locked）
        if summary["matched"] % 10 == 0:
            db.commit()

    # 「谁没交」只能靠整班导出来判断。
    # 按人抓取（勾了具体学生）时，包里本来就只有那几个人，不能拿它去说别人没交，
    # 否则会把之前已经抓过、已经评过的学生覆盖成「未交」。
    if export.person_ids:
        summary["missing"] = None  # 前端不用显示
    else:
        for student in students:
            if student.id in matched_ids:
                continue
            submission = _upsert_submission(db, work, student)
            submission.state = SubmissionState.missing
            submission.file_state = FileState.none
            # 之前抓过、这次没出现在包里的（学生撤销了提交），把文件信息一并清干净，
            # 免得出现「状态是未交、却还有图片」这种自相矛盾的情况
            submission.source_filename = None
            submission.object_key = None
            submission.size_bytes = 0
            submission.checksum = None
            submission.content_checksum = None
            submission.image_count = 0
            submission.extracted_text = None
            submission.error_message = None
            summary["missing"] += 1

    db.flush()

    # 同一个 work 里出现多次的图片，基本都是老师发的题面，标成 question
    rows = (
        db.execute(
            select(IstudySubmissionFile)
            .join(IstudySubmission, IstudySubmission.id == IstudySubmissionFile.submission_id)
            .where(IstudySubmission.work_id == work.id)
        )
        .scalars()
        .all()
    )
    counts = Counter(row.checksum for row in rows if row.checksum)
    for row in rows:
        row.role = "question" if row.checksum and counts[row.checksum] > 1 else "answer"

    export.file_count = summary["matched"]
    return summary


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        # 大部分时间是在等 i学习 打包，多开几个线程让多班级一起排队
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="istudy-export")
    return _executor


def run_export_job(export_id: int) -> None:
    """在后台线程里跑完整条「导出 → 下载 → 解包 → 入库」的链路。"""
    db = SessionLocal()
    try:
        export = db.get(IstudyExport, export_id)
        if export is None:
            logger.warning("导出任务 %s 不存在", export_id)
            return
        work = export.work
        course = work.course if work else None
        if work is None or course is None:
            raise IstudyError("导出任务关联的作业或课程不在了")
        if not (course.istudy_cid and course.istudy_cpi):
            raise IstudyError("这门课还没绑定 i学习 课程，请先同步一次作业列表")

        export.status = ExportStatus.exporting
        export.started_at = datetime.now()
        export.message = "已提交给 i学习，正在打包（一个班通常要几分钟）"
        db.commit()

        download_id, data = asyncio.run(
            run_export_remote(course.istudy_cid, course.istudy_cpi, work, export)
        )
        export.istudy_download_id = download_id
        export.status = ExportStatus.downloading
        export.message = f"已下载 {len(data) / 1024 / 1024:.1f} MB，正在解包"
        db.commit()

        export.status = ExportStatus.parsing
        export.message = "正在解包、提取作答图片并匹配学生"
        db.commit()

        summary = persist_export_bundle(db, export, data, zip_name=f"{work.name}.zip")
        export.status = ExportStatus.done
        export.finished_at = datetime.now()
        export.message = f"抓到 {summary['matched']} 名学生"
        if summary["missing"]:
            export.message += f"（未交 {summary['missing']} 人）"
        db.commit()
        logger.info("作业导出完成 export_id=%s %s", export_id, export.message)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("导出作业附件失败 export_id=%s", export_id)
        try:
            export = db.get(IstudyExport, export_id)
            if export is not None:
                export.status = ExportStatus.failed
                export.message = str(exc)
                export.finished_at = datetime.now()
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("记录导出失败状态时又出错了 export_id=%s", export_id)
    finally:
        db.close()


def _run_export_safe(export_id: int) -> None:
    try:
        run_export_job(export_id)
    except Exception:  # noqa: BLE001
        logger.exception("后台导出任务异常 export_id=%s", export_id)


def enqueue_export_job(export_id: int) -> None:
    _get_executor().submit(_run_export_safe, export_id)


def shutdown_export_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=False)
        _executor = None
