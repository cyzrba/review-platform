"""上传提交、匹配学生、触发 AI 评审、查看/修正评分结果。"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Class, Course, CourseClass, GradingResult, ResultStatus, Rubric, Student
from ..pagination import Page, PageParams, fetch_page, make_page, page_params
from ..schemas import (
    GradeRunRequest,
    GradeRunResult,
    GradingResultOut,
    GradingResultUpdate,
    UploadIssueOut,
    UploadSummary,
)
from ..serializers import RESULT_LOAD_OPTIONS, result_out
from ..services.archive import (
    KIND_RULES,
    StudentRef,
    assign_students,
    kind_rule,
    scan_uploads,
    select_documents,
    trim_issues,
)
from ..services.grading import enqueue_grading, grade_result
from ..storage import get_storage
from ..utils import guess_content_type, safe_filename, sha256_hex

logger = logging.getLogger(__name__)

router = APIRouter(tags=["评审与评分结果"])


def _get_rubric(db: Session, rubric_id: int) -> Rubric:
    obj = db.get(Rubric, rubric_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="评分细则不存在")
    return obj


def _candidate_students(
    db: Session, class_id: int | None, course_id: int | None
) -> list[StudentRef]:
    """参与匹配的学生范围：可限定到某个班级，或某个课程下的所有班级。"""
    stmt = (
        select(Student.id, Student.student_no, Student.name, Class.name)
        .join(Class, Class.id == Student.class_id)
    )
    if class_id is not None:
        stmt = stmt.where(Student.class_id == class_id)
    if course_id is not None:
        stmt = stmt.join(CourseClass, CourseClass.class_id == Student.class_id).where(
            CourseClass.course_id == course_id
        )
    return [
        StudentRef(id=row[0], student_no=row[1], name=row[2], class_name=row[3] or "")
        for row in db.execute(stmt).all()
    ]


# --------------------------------------------------------------------------- #
# 上传与匹配
# --------------------------------------------------------------------------- #
@router.post(
    "/rubrics/{rubric_id}/submissions",
    response_model=UploadSummary,
    summary="上传提交文件（压缩包或单文件），自动匹配学生",
)
async def upload_submissions(
    rubric_id: int,
    files: list[UploadFile] = File(..., description="压缩包或单个文件，可多选"),
    class_id: int | None = Form(None, description="限定只在该班级里匹配学生，可选"),
    course_id: int | None = Form(None, description="限定只在该课程下的班级里匹配，可选"),
    db: Session = Depends(get_db),
) -> UploadSummary:
    rubric = _get_rubric(db, rubric_id)
    rule = kind_rule(rubric.kind.value)

    uploads: list[tuple[str, bytes]] = []
    for upload in files:
        data = await upload.read()
        raw_name = upload.filename or "file"
        if not data:
            raise HTTPException(status_code=400, detail=f"文件 {raw_name} 是空的")
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413, detail=f"文件 {raw_name} 超过 {settings.max_upload_mb}MB 上限"
            )
        uploads.append((safe_filename(raw_name), data))

    if not uploads:
        raise HTTPException(status_code=400, detail="请至少选择一个文件")
    if course_id is not None and db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")

    students = _candidate_students(db, class_id, course_id)
    if not students:
        scope = "该课程下" if course_id else ("该班级" if class_id else "系统里")
        raise HTTPException(status_code=400, detail=f"{scope}还没有学生，请先在课程下导入名单")

    scan = scan_uploads(uploads)
    documents, skipped = select_documents(scan.leaves, rubric.kind.value)
    issues = [*scan.issues, *skipped]

    if not documents:
        return UploadSummary(
            rubric_id=rubric.id,
            kind=rubric.kind,
            scanned_files=len(scan.leaves),
            accepted_files=0,
            issues=[
                UploadIssueOut(filename=item.filename, reason=item.reason)
                for item in trim_issues(issues)
            ],
            message=f"没有找到可评审的文件。{rule['expect']}",
        )

    matched, match_issues = assign_students(documents, students)
    issues.extend(match_issues)

    created = updated = 0
    storage = get_storage()
    for entry in matched:
        key = f"rubrics/{rubric.id}/results/{uuid.uuid4().hex}/{entry.leaf.name}"
        content_type = guess_content_type(entry.leaf.name)
        storage.put_bytes(key, entry.leaf.data, content_type)

        student = db.get(Student, entry.student_id)
        if student is None:
            continue

        existing = db.execute(
            select(GradingResult).where(
                GradingResult.rubric_id == rubric.id,
                GradingResult.student_id == entry.student_id,
            )
        ).scalars().first()

        if existing is None:
            db.add(
                GradingResult(
                    rubric_id=rubric.id,
                    student_id=entry.student_id,
                    class_id=student.class_id,
                    source_filename=entry.leaf.name,
                    object_key=key,
                    content_type=content_type,
                    size_bytes=entry.leaf.size,
                    checksum=sha256_hex(entry.leaf.data),
                    match_reason=entry.reason,
                    status=ResultStatus.pending,
                )
            )
            created += 1
            continue

        # 同一个学生重新上传：覆盖文件并重置评审结果（旧文件保留在对象存储里备查）
        existing.class_id = student.class_id
        existing.source_filename = entry.leaf.name
        existing.object_key = key
        existing.content_type = content_type
        existing.size_bytes = entry.leaf.size
        existing.checksum = sha256_hex(entry.leaf.data)
        existing.match_reason = entry.reason
        existing.status = ResultStatus.pending
        existing.score = None
        existing.comment = None
        existing.graded_at = None
        existing.error_message = None
        existing.is_manual = False
        updated += 1

    db.commit()

    parts = [f"扫到 {len(scan.leaves)} 个文件，其中可评审 {len(documents)} 个"]
    parts.append(f"匹配到 {len(matched)} 名学生（新增 {created}，覆盖 {updated}）")
    if issues:
        parts.append(f"{len(issues)} 个文件被跳过")
    return UploadSummary(
        rubric_id=rubric.id,
        kind=rubric.kind,
        scanned_files=len(scan.leaves),
        accepted_files=len(documents),
        matched=len(matched),
        created=created,
        updated=updated,
        issues=[
            UploadIssueOut(filename=item.filename, reason=item.reason)
            for item in trim_issues(issues)
        ],
        message="；".join(parts),
    )


@router.get("/rubrics/{rubric_id}/upload-hint", summary="该细则要求的文件命名格式")
def upload_hint(rubric_id: int, db: Session = Depends(get_db)) -> dict:
    rubric = _get_rubric(db, rubric_id)
    rule = kind_rule(rubric.kind.value)
    return {
        "kind": rubric.kind.value,
        "label": rule["label"],
        "expect": rule["expect"],
        "accepted": sorted(rule["accepted"]),
        "all_kinds": {
            key: {"label": value["label"], "expect": value["expect"]}
            for key, value in KIND_RULES.items()
        },
    }


# --------------------------------------------------------------------------- #
# 触发评审
# --------------------------------------------------------------------------- #
def _select_pending(
    db: Session, rubric_id: int, payload: GradeRunRequest
) -> list[GradingResult]:
    stmt = select(GradingResult).where(GradingResult.rubric_id == rubric_id)
    if payload.result_ids:
        stmt = stmt.where(GradingResult.id.in_(payload.result_ids))
    elif not payload.force:
        stmt = stmt.where(GradingResult.status.in_([ResultStatus.pending, ResultStatus.failed]))
    if payload.class_id is not None:
        stmt = stmt.where(GradingResult.class_id == payload.class_id)
    if payload.course_id is not None:
        stmt = stmt.join(CourseClass, CourseClass.class_id == GradingResult.class_id).where(
            CourseClass.course_id == payload.course_id
        )
    return list(db.execute(stmt).scalars().all())


@router.post("/rubrics/{rubric_id}/grade", response_model=GradeRunResult, summary="触发 AI 评审")
def run_grading(
    rubric_id: int,
    payload: GradeRunRequest | None = None,
    sync: bool = Query(False, description="true 则同步执行（小批量调试用）"),
    db: Session = Depends(get_db),
) -> GradeRunResult:
    rubric = _get_rubric(db, rubric_id)
    if not rubric.criteria and not rubric.description:
        raise HTTPException(status_code=400, detail="请先在评分细则里填写评分标准，再发起评审")

    request = payload or GradeRunRequest()
    targets = _select_pending(db, rubric_id, request)
    if not targets:
        return GradeRunResult(queued=0, message="没有需要评审的记录")

    ids = [item.id for item in targets]
    if sync:
        for result_id in ids:
            grade_result(result_id)
        return GradeRunResult(queued=len(ids), result_ids=ids, message="已同步完成评审")

    for item in targets:
        item.status = ResultStatus.grading
    db.commit()
    enqueue_grading(ids)
    return GradeRunResult(queued=len(ids), result_ids=ids, message="已提交后台评审")


@router.post(
    "/grading-results/{result_id}/grade", response_model=GradeRunResult, summary="评审单条记录"
)
def run_single_grading(
    result_id: int,
    sync: bool = Query(False),
    db: Session = Depends(get_db),
) -> GradeRunResult:
    result = db.get(GradingResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="评分结果不存在")
    if sync:
        grade_result(result_id)
        return GradeRunResult(queued=1, result_ids=[result_id], message="已同步完成评审")
    result.status = ResultStatus.grading
    db.commit()
    enqueue_grading([result_id])
    return GradeRunResult(queued=1, result_ids=[result_id], message="已提交后台评审")


# --------------------------------------------------------------------------- #
# 评分结果
# --------------------------------------------------------------------------- #
@router.get(
    "/rubrics/{rubric_id}/results",
    response_model=Page[GradingResultOut],
    summary="评分结果列表（支持分页，可按课程 / 班级 / 状态筛选）",
)
def list_results(
    rubric_id: int,
    class_id: int | None = None,
    course_id: int | None = Query(None, description="只看该课程下班级的结果"),
    status: ResultStatus | None = None,
    q: str | None = Query(None, description="学号 / 姓名模糊搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[GradingResultOut]:
    _get_rubric(db, rubric_id)
    stmt = (
        select(GradingResult)
        .options(*RESULT_LOAD_OPTIONS)
        .join(Student, Student.id == GradingResult.student_id)
        .join(Class, Class.id == GradingResult.class_id)
        .where(GradingResult.rubric_id == rubric_id)
    )
    if class_id is not None:
        stmt = stmt.where(GradingResult.class_id == class_id)
    if course_id is not None:
        stmt = stmt.join(
            CourseClass, CourseClass.class_id == GradingResult.class_id
        ).where(CourseClass.course_id == course_id)
    if status is not None:
        stmt = stmt.where(GradingResult.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Student.student_no.like(like), Student.name.like(like)))
    stmt = stmt.order_by(Class.name, Student.student_no)
    rows, total = fetch_page(db, stmt, params)
    return make_page([result_out(item) for item in rows], total, params)


@router.get("/grading-results/{result_id}", response_model=GradingResultOut, summary="单条评分结果")
def get_result(result_id: int, db: Session = Depends(get_db)) -> GradingResultOut:
    obj = db.get(GradingResult, result_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="评分结果不存在")
    return result_out(obj)


@router.patch(
    "/grading-results/{result_id}", response_model=GradingResultOut, summary="人工修正分数或评语"
)
def update_result(
    result_id: int, payload: GradingResultUpdate, db: Session = Depends(get_db)
) -> GradingResultOut:
    obj = db.get(GradingResult, result_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="评分结果不存在")
    data = payload.model_dump(exclude_unset=True)
    if "score" in data and data["score"] is not None:
        limit = obj.rubric.total_score or 100.0
        obj.score = max(0.0, min(float(data["score"]), float(limit)))
    if "comment" in data:
        obj.comment = data["comment"]
    obj.is_manual = True
    if obj.status in (ResultStatus.pending, ResultStatus.failed) and obj.score is not None:
        obj.status = ResultStatus.graded
    db.commit()
    db.refresh(obj)
    return result_out(obj)


@router.delete("/grading-results/{result_id}", status_code=204, summary="删除一条评分结果")
def delete_result(result_id: int, db: Session = Depends(get_db)) -> Response:
    obj = db.get(GradingResult, result_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="评分结果不存在")
    db.delete(obj)
    db.commit()
    return Response(status_code=204)


@router.get(
    "/rubrics/{rubric_id}/class-summary",
    response_model=Page[dict],
    summary="按班级汇总该细则的成绩（支持分页）",
)
def class_summary(
    rubric_id: int,
    course_id: int | None = Query(None, description="只看该课程下的班级"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[dict]:
    _get_rubric(db, rubric_id)
    graded = func.sum(case((GradingResult.status == ResultStatus.graded, 1), else_=0))
    failed = func.sum(case((GradingResult.status == ResultStatus.failed, 1), else_=0))
    stmt = (
        select(
            Class.id,
            Class.department,
            Class.major,
            Class.name,
            func.count(GradingResult.id).label("total"),
            graded.label("graded"),
            failed.label("failed"),
            func.avg(GradingResult.score).label("average_score"),
            func.max(GradingResult.score).label("max_score"),
            func.min(GradingResult.score).label("min_score"),
        )
        .join(Class, Class.id == GradingResult.class_id)
        .where(GradingResult.rubric_id == rubric_id)
        .group_by(Class.id, Class.department, Class.major, Class.name)
    )
    if course_id is not None:
        stmt = stmt.join(CourseClass, CourseClass.class_id == Class.id).where(
            CourseClass.course_id == course_id
        )
    stmt = stmt.order_by(Class.department, Class.major, Class.name)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.offset(params.offset).limit(params.page_size)).all()
    items = []
    for row in rows:
        count = row[4] or 0
        done = row[5] or 0
        bad = row[6] or 0
        items.append(
            {
                "class_id": row[0],
                "department": row[1],
                "major": row[2],
                "class_name": row[3],
                "full_name": "-".join(part for part in (row[1], row[2], row[3]) if part),
                "total": count,
                "graded": done,
                "failed": bad,
                "pending": max(count - done - bad, 0),
                "average_score": round(float(row[7]), 2) if row[7] is not None else None,
                "max_score": float(row[8]) if row[8] is not None else None,
                "min_score": float(row[9]) if row[9] is not None else None,
            }
        )
    return make_page(items, total, params)
