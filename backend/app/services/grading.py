"""把「调用评审器」和「写入评分结果」串起来。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..database import session_scope
from ..models import GradingResult, ResultStatus
from ..storage import get_storage
from .document import DocumentParseError, extract_text
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
    )


def grade_result(result_id: int, *, reviewer_name: str | None = None) -> None:
    """评审一条评分结果（独立会话，可安全跑在后台线程里）。"""
    started = time.perf_counter()
    with session_scope() as db:
        result = db.get(GradingResult, result_id)
        if result is None:
            logger.warning("评分结果 %s 不存在，跳过", result_id)
            return

        result.status = ResultStatus.grading
        db.flush()

        try:
            outcome = get_reviewer(reviewer_name).review(_build_context(db, result))
        except Exception as exc:  # noqa: BLE001
            logger.exception("评分结果 %s 评审失败", result_id)
            result.status = ResultStatus.failed
            result.error_message = str(exc)
            return

        limit = result.rubric.total_score or 100.0
        result.score = max(0.0, min(float(outcome.total_score), float(limit)))
        result.comment = outcome.comment
        result.model = outcome.model
        result.prompt_version = outcome.prompt_version
        result.duration_ms = outcome.duration_ms or int((time.perf_counter() - started) * 1000)
        result.raw_response = outcome.raw_response
        result.graded_at = datetime.now()
        result.status = ResultStatus.graded
        result.error_message = None
        result.is_manual = False


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
