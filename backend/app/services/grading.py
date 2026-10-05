"""把「调用评审器」和「写入评分结果」串起来。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import session_scope, with_retry
from ..models import GradingResult, IstudyWork, ResultStatus
from ..storage import get_storage
from .answer_files import prepare_for_llm
from .document import DocumentParseError, extract_text
from .llm_client import ImagePart
from .reviewer import ReviewContext, get_reviewer

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=max(1, settings.ai_review_workers), thread_name_prefix="ai-review"
        )
    return _executor


def _build_context(db: Session, result: GradingResult) -> ReviewContext:
    rubric = result.rubric
    student = result.student
    images = _collect_answer_images(db, result)

    if images:
        # 从 i学习 抓下来的提交：已经把题面和作答抽成图片了，正文直接用当时抽好的，
        # 不用再去读原始 .doc（读了也解析不了，只会刷一堆 INFO 日志）
        submission = _find_istudy_submission(db, result) or _find_lab_submission(db, result)
        text = submission.extracted_text if submission is not None else None
        payload = b""
    else:
        # 手工上传的提交：还是走「读文件 + 抽文本」的老路
        payload = get_storage().get_bytes(result.object_key)
        try:
            text = extract_text(result.source_filename, result.content_type, payload)
        except DocumentParseError as exc:
            logger.info("未能抽取文本（%s）：%s", result.source_filename, exc)
            text = None

    return ReviewContext(
        rubric_name=rubric.name,
        kind=rubric.kind.value,
        rubric_description=rubric.description,
        criteria=rubric.criteria,
        extra_prompt=rubric.extra_prompt,
        total_score=rubric.total_score,
        student_no=student.student_no,
        student_name=student.name,
        filename=result.source_filename,
        content_type=result.content_type,
        file_bytes=payload,
        text=text,
        images=images,
        images_sent=len(images),
    )


def _find_istudy_submission(db: Session, result: GradingResult):
    """这条评分结果对应的「i学习 作业」提交记录（不是作业就返回 None）。"""
    from ..models import IstudySubmission

    return db.execute(
        select(IstudySubmission)
        .join(IstudyWork, IstudyWork.id == IstudySubmission.work_id)
        .where(
            IstudyWork.rubric_id == result.rubric_id,
            IstudySubmission.student_id == result.student_id,
        )
    ).scalars().first()


def _find_lab_submission(db: Session, result: GradingResult):
    """这条评分结果对应的「i学习 实验报告」提交记录（不是实验报告就返回 None）。"""
    from ..models import IstudyLabReport, IstudyLabSubmission

    return db.execute(
        select(IstudyLabSubmission)
        .join(IstudyLabReport, IstudyLabReport.id == IstudyLabSubmission.report_id)
        .where(
            IstudyLabReport.rubric_id == result.rubric_id,
            IstudyLabSubmission.student_id == result.student_id,
        )
    ).scalars().first()


def collect_answer_image_rows(db: Session, result: GradingResult) -> list[tuple[object, str]]:
    """按「先题面、后作答」的顺序，列出这个学生的图片（返回数据库记录 + 角色）。

    题面图是全班同一份，直接从这次作业里取一份（不用依赖这个学生自己有没有交题面）。
    评审和前端预览都用这一份逻辑，保证「看到的」和「喂给模型的」是同一批图。

    实验报告不一样：学生交的整份 PDF 里就带着报告模板和题目，所以直接按页顺序全算作答。
    """
    from ..models import IstudySubmission, IstudySubmissionFile, IstudyWork

    submission = _find_istudy_submission(db, result)
    if submission is None:
        lab = _find_lab_submission(db, result)
        if lab is None:
            return []
        return [(item, "answer") for item in lab.files]

    work = db.get(IstudyWork, submission.work_id)
    if work is None:
        return []

    # 题面：这次作业里 role=question 的图，去重后取最早的几张
    question_rows = db.execute(
        select(IstudySubmissionFile)
        .join(IstudySubmission, IstudySubmission.id == IstudySubmissionFile.submission_id)
        .where(
            IstudySubmission.work_id == work.id,
            IstudySubmissionFile.role == "question",
        )
        .order_by(IstudySubmissionFile.seq)
    ).scalars().all()
    seen: set[str] = set()
    questions: list[IstudySubmissionFile] = []
    for row in question_rows:
        if row.checksum and row.checksum in seen:
            continue
        if row.checksum:
            seen.add(row.checksum)
        questions.append(row)

    own = [item for item in submission.files if item.role != "question"]
    if not own:
        own = list(submission.files)

    rows: list[tuple[object, str]] = [(item, "question") for item in questions]
    rows += [(item, "answer") for item in own]
    return rows


def _collect_answer_images(db: Session, result: GradingResult) -> list[ImagePart]:
    """把图片取出来、压好，准备发给模型。"""
    storage = get_storage()
    images: list[ImagePart] = []
    for row, role in collect_answer_image_rows(db, result):
        if len(images) >= settings.llm_max_images:
            break
        try:
            raw = storage.get_bytes(row.object_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读不到图片 %s：%s", row.object_key, exc)
            continue
        prepared, content_type = prepare_for_llm(
            raw,
            max_edge=settings.llm_image_max_edge,
            quality=settings.llm_image_quality,
        )
        images.append(
            ImagePart(
                filename=row.filename,
                content_type=content_type,
                data=prepared,
                role=role,
            )
        )
    return images


def _mark_grading(result_id: int) -> ReviewContext | None:
    """把状态标成「评审中」，顺手把评审要用的上下文准备好，然后立刻提交、放开数据库锁。"""
    with session_scope() as db:
        result = db.get(GradingResult, result_id)
        if result is None:
            return None
        result.status = ResultStatus.grading
        db.flush()
        return _build_context(db, result)


def _mark_failed(result_id: int, message: str) -> None:
    with session_scope() as db:
        result = db.get(GradingResult, result_id)
        if result is None:
            return
        result.status = ResultStatus.failed
        result.error_message = message


def _save_outcome(result_id: int, outcome, started: float) -> None:  # noqa: ANN001
    with session_scope() as db:
        result = db.get(GradingResult, result_id)
        if result is None:
            return
        limit = result.rubric.total_score or 100.0
        result.score = max(0.0, min(float(outcome.total_score), float(limit)))
        result.comment = outcome.comment
        result.model = outcome.model
        result.prompt_version = outcome.prompt_version
        result.duration_ms = outcome.duration_ms or int((time.perf_counter() - started) * 1000)
        result.raw_response = outcome.raw_response
        result.prompt_tokens = outcome.prompt_tokens
        result.completion_tokens = outcome.completion_tokens
        result.images_sent = outcome.images_sent
        result.graded_at = datetime.now()
        result.status = ResultStatus.graded
        result.error_message = None
        result.is_manual = False


def grade_result(result_id: int, *, reviewer_name: str | None = None) -> None:
    """评审一条评分结果（独立会话，可安全跑在后台线程里）。

    分三步走，中间那步（调模型，要几秒到几十秒）**不占数据库**：
      1. 短事务：标「评审中」+ 准备上下文 → 提交
      2. 调模型
      3. 短事务：写分数评语 → 提交
    早期版本把写事务一直开着去调模型，两个后台任务同时跑就会互相把锁等死
    （`database is locked`）。
    """
    started = time.perf_counter()
    context = with_retry(lambda: _mark_grading(result_id), what=f"标记评审中 id={result_id}")
    if context is None:
        logger.warning("评分结果 %s 不存在，跳过", result_id)
        return

    try:
        outcome = get_reviewer(reviewer_name).review(context)
    except Exception as exc:  # noqa: BLE001
        logger.exception("评分结果 %s 评审失败", result_id)
        try:
            with_retry(lambda: _mark_failed(result_id, str(exc)), what=f"标记失败 id={result_id}")
        except Exception:  # noqa: BLE001
            logger.exception("连失败状态都没写进去 result_id=%s", result_id)
        return

    with_retry(lambda: _save_outcome(result_id, outcome, started), what=f"写评审结果 id={result_id}")


def _grade_safe(result_id: int) -> None:
    try:
        grade_result(result_id)
    except Exception:  # noqa: BLE001
        logger.exception("后台评审任务异常 result_id=%s", result_id)


def enqueue_grading(result_ids: list[int]) -> None:
    executor = _get_executor()
    for result_id in result_ids:
        executor.submit(_grade_safe, result_id)


def shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=False)
        _executor = None
