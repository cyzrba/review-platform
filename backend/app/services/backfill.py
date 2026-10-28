"""把本地 AI 评分结果（分数 + 评语）一键回填到 i学习 作业批阅。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    GradingResult,
    IstudySubmission,
    IstudyWork,
    ResultStatus,
    Student,
)
from . import homework_sync
from .istudy import IstudyClient, IstudyError, distribute_total, review_qids

logger = logging.getLogger(__name__)

PENDING_LABEL = "待批阅"


def _is_pending(state: str) -> bool:
    return PENDING_LABEL in (state or "")


def _works_for(db: Session, work: IstudyWork, class_id: int | None) -> list[IstudyWork]:
    works = homework_sync.assignment_works(db, work)
    if class_id is not None:
        works = [item for item in works if item.class_id == class_id]
    return works


def _course_ids(work: IstudyWork) -> tuple[str, str]:
    course = work.course
    if course is None or not course.istudy_cid or not course.istudy_cpi:
        raise IstudyError("这门课还没绑定 i学习 课程，请先同步一次作业列表")
    return course.istudy_cid, course.istudy_cpi


async def _live_states(
    client: IstudyClient, works: list[IstudyWork], *, cid: str, cpi: str
) -> dict[tuple[int, str], dict[str, str]]:
    """按班级取 i学习 现行批阅状态：``{(work_id, answer_id): 行}``。"""
    out: dict[tuple[int, str], dict[str, str]] = {}
    for item in works:
        try:
            rows = await client.list_submitted_students(
                cid, cpi, item.istudy_clazzid, item.istudy_work_id
            )
        except IstudyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IstudyError(f"读取「{item.name}」批阅名单失败：{exc}") from exc
        for row in rows:
            out[(item.id, str(row.get("answer_id") or ""))] = row
    return out


async def build_preview(
    db: Session,
    work: IstudyWork,
    *,
    class_id: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """生成回填预览：每个学生要写多少分、当前 i学习 状态、会写还是跳过。"""
    works = _works_for(db, work, class_id)
    if not works:
        raise IstudyError("这次作业下没有可选班级")
    cid, cpi = _course_ids(work)

    rows: list[dict[str, Any]] = []
    async with IstudyClient() as client:
        states = await _live_states(client, works, cid=cid, cpi=cpi)

    for item in works:
        rubric_id = item.rubric_id
        submissions = (
            db.execute(
                select(IstudySubmission).where(IstudySubmission.work_id == item.id)
            )
            .scalars()
            .all()
        )
        by_answer = {
            str(sub.istudy_answer_id): sub
            for sub in submissions
            if sub.istudy_answer_id
        }
        students = (
            db.execute(
                select(Student).where(Student.class_id == item.class_id)
                if item.class_id
                else select(Student).where(Student.id.in_([s.student_id for s in submissions] or [-1]))
            )
            .scalars()
            .all()
        )
        by_no = {student.student_no: student for student in students}
        by_id = {student.id: student for student in students}

        results = {}
        if rubric_id:
            results = {
                row.student_id: row
                for row in db.execute(
                    select(GradingResult).where(
                        GradingResult.rubric_id == rubric_id,
                        GradingResult.student_id.in_(list(by_id) or [-1]),
                    )
                )
                .scalars()
                .all()
            }

        live_rows = [
            row for (wid, _), row in states.items() if wid == item.id
        ]
        for live in live_rows:
            answer_id = str(live.get("answer_id") or "")
            sub = by_answer.get(answer_id)
            student = by_id.get(sub.student_id) if sub else None
            student = student or by_no.get(str(live.get("student_no") or ""))
            state = str(live.get("state") or "")
            raw_istudy_score = str(live.get("istudy_score") or "").strip()
            try:
                istudy_score = float(raw_istudy_score) if raw_istudy_score else None
            except ValueError:
                istudy_score = None
            # 待批阅的行 i学习 默认给 0，这不是分数，展示时按「还没有分数」处理
            if istudy_score == 0 and _is_pending(state):
                istudy_score = None

            row: dict[str, Any] = {
                "student_id": student.id if student else None,
                "student_no": (student.student_no if student else str(live.get("student_no") or "")),
                "student_name": (student.name if student else str(live.get("name") or "")),
                "class_id": item.class_id,
                "class_name": item.class_.name if item.class_ else "",
                "work_id": item.id,
                "answer_id": answer_id,
                "local_score": None,
                "local_comment": None,
                "istudy_state": state,
                "istudy_score": istudy_score,
                "action": "skip",
                "reason": "",
                "backfill_status": None,
            }

            if student is None:
                row["reason"] = "本地名单里没有这个学生"
                rows.append(row)
                continue

            result = results.get(student.id)
            if result is not None:
                row["local_score"] = result.score
                row["local_comment"] = result.comment
                row["backfill_status"] = result.istudy_backfill_status

            if not answer_id:
                row["reason"] = "没有 i学习 作答记录"
            elif result is None:
                row["reason"] = "还没有评分结果"
            elif result.status != ResultStatus.graded or result.score is None:
                row["reason"] = "还没评审完"
            elif not _is_pending(state) and not overwrite:
                existing = f"，现有 {istudy_score:g} 分" if istudy_score is not None else ""
                row["reason"] = (
                    f"i学习 已批阅（{state or '未知状态'}{existing}），勾选覆盖才会写"
                )
            else:
                row["action"] = "write"
                row["reason"] = "覆盖已有分数" if not _is_pending(state) else "写入分数"
            rows.append(row)

    rows.sort(key=lambda item: (item["class_name"], item["student_no"]))
    writable = sum(1 for row in rows if row["action"] == "write")
    return {
        "work_id": work.id,
        "work_name": work.name,
        "class_id": class_id,
        "overwrite": overwrite,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "write": writable,
            "skip": len(rows) - writable,
        },
    }


def _pick_submission(
    db: Session, student_id: int, works: list[IstudyWork]
) -> tuple[IstudyWork, IstudySubmission] | None:
    work_ids = [item.id for item in works]
    submissions = (
        db.execute(
            select(IstudySubmission).where(
                IstudySubmission.student_id == student_id,
                IstudySubmission.work_id.in_(work_ids or [-1]),
            )
        )
        .scalars()
        .all()
    )
    if not submissions:
        return None
    by_work = {item.id: item for item in works}
    sub = next((item for item in submissions if item.istudy_answer_id), submissions[0])
    return by_work.get(sub.work_id), sub


async def apply_backfill(
    db: Session,
    work: IstudyWork,
    *,
    student_ids: list[int],
    overwrite: bool = False,
    write_comment: bool = True,
) -> dict[str, Any]:
    """逐人回填，所有人都会返回一条回执（成功 / 失败都写回数据库）。"""
    works = _works_for(db, work, None)
    cid, cpi = _course_ids(work)
    if not student_ids:
        raise IstudyError("没有选中要回填的学生")

    receipts: list[dict[str, Any]] = []
    async with IstudyClient() as client:
        states = await _live_states(client, works, cid=cid, cpi=cpi)
        # 本地的 istudy_answer_id 可能一直是空的（历史导出链路没有回写过这个字段），
        # 真正可靠的来源是 i学习 批阅名单：按（作业行, 学号）反查这次作答的 id。
        live_by_student = {
            (work_id, str(row.get("student_no") or "")): row
            for (work_id, _answer_id), row in states.items()
        }
        for student_id in student_ids:
            picked = _pick_submission(db, student_id, works)
            if picked is None:
                receipts.append(
                    {
                        "student_id": student_id,
                        "student_no": "",
                        "student_name": "",
                        "ok": False,
                        "score": None,
                        "message": "本地没有这个学生的 i学习 作答记录",
                    }
                )
                continue

            item, submission = picked
            student = db.get(Student, student_id)
            result = None
            if item.rubric_id:
                result = db.execute(
                    select(GradingResult).where(
                        GradingResult.rubric_id == item.rubric_id,
                        GradingResult.student_id == student_id,
                    )
                ).scalar_one_or_none()

            answer_id = str(submission.istudy_answer_id or "")
            name = student.name if student else ""
            no = student.student_no if student else ""
            live = live_by_student.get((item.id, no)) or {}
            answer_id = str(live.get("answer_id") or answer_id)
            state = str(live.get("state") or "")

            def fail(message: str) -> dict[str, Any]:
                if result is not None:
                    result.istudy_backfill_status = "failed"
                    result.istudy_backfill_message = message
                    result.istudy_backfilled_at = datetime.now()
                    db.commit()
                logger.warning("回填失败 %s(%s)：%s", name, no, message)
                return {
                    "student_id": student_id,
                    "student_no": no,
                    "student_name": name,
                    "ok": False,
                    "score": None,
                    "message": message,
                }

            if result is None:
                receipts.append(fail("还没有评分结果"))
                continue
            if not answer_id:
                receipts.append(
                    fail("i学习 批阅名单里没有这位学生的作答记录（可能已撤回或换班）")
                )
                continue
            if result.status != ResultStatus.graded or result.score is None:
                receipts.append(fail("还没评审完"))
                continue
            if not _is_pending(state) and not overwrite:
                receipts.append(fail(f"i学习 已批阅（{state or '未知状态'}），未开启覆盖"))
                continue

            if submission.istudy_answer_id != answer_id:
                # 顺手把答案 id 补回本地，后面预览 / 排查都能直接用
                submission.istudy_answer_id = answer_id
                db.commit()

            try:
                fields, referer = await client.fetch_review_form(
                    cid=cid,
                    cpi=cpi,
                    clazzid=item.istudy_clazzid,
                    work_id=item.istudy_work_id,
                    answer_id=answer_id,
                )
                qids = review_qids(fields)
                full_scores: list[float] = []
                for qid in qids:
                    raw = (fields.get(f"qscore{qid}") or ["0"])[0]
                    try:
                        full_scores.append(float(raw))
                    except (TypeError, ValueError):
                        full_scores.append(0.0)

                cap = sum(full_scores) if sum(full_scores) > 0 else float(
                    result.rubric.total_score or 100
                )
                score = max(0.0, min(float(result.score), cap))
                shares = distribute_total(score, full_scores)
                await client.save_review(
                    fields,
                    score=score,
                    referer=referer,
                    comment=result.comment if write_comment else None,
                    per_scores=dict(zip(qids, shares)),
                )
            except Exception as exc:  # noqa: BLE001
                receipts.append(fail(str(exc)))
                continue

            result.istudy_backfill_status = "success"
            result.istudy_backfill_score = score
            result.istudy_backfilled_at = datetime.now()
            result.istudy_backfill_message = "已写入 i学习"
            submission.istudy_score = score
            db.commit()
            receipts.append(
                {
                    "student_id": student_id,
                    "student_no": no,
                    "student_name": name,
                    "ok": True,
                    "score": score,
                    "message": "已写入 i学习",
                }
            )

    ok = sum(1 for item in receipts if item["ok"])
    return {
        "work_id": work.id,
        "work_name": work.name,
        "receipts": receipts,
        "summary": {"total": len(receipts), "success": ok, "failed": len(receipts) - ok},
    }
