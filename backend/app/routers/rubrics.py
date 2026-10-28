"""评分细则管理。

一个项目 / 一次作业对应一份评分细则，不分版本、不分维度：
只需要名称、类型（作业 / 实验报告）、任务说明、评分标准正文和满分。
细则可以挂到一个或多个课程下（课程-评分标准关联表）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import (
    Class,
    Course,
    CourseRubric,
    GradingKind,
    GradingResult,
    IstudyLabReport,
    IstudyWork,
    Rubric,
)
from ..pagination import Page, PageParams, fetch_page, make_page, page_params
from ..schemas import RubricCreate, RubricMaterialPreviewOut, RubricOut, RubricUpdate
from ..serializers import rubric_out
from ..services.rubric_materials import (
    ATTACHMENT_ROLES,
    RubricMaterialError,
    delete_files,
    delete_object_keys,
    page_keys,
    prepare_attachment,
)

router = APIRouter(tags=["评分细则"])

_RUBRIC_LOAD = (
    selectinload(Rubric.course_links).selectinload(CourseRubric.course),
    selectinload(Rubric.attachments),
)


def get_rubric_or_404(db: Session, rubric_id: int) -> Rubric:
    obj = db.get(Rubric, rubric_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="评分细则不存在")
    return obj


def _sync_courses(db: Session, rubric: Rubric, course_ids: list[int]) -> None:
    """把细则的课程关联替换成给定集合。"""
    for link in list(rubric.course_links):
        db.delete(link)
    rubric.course_links.clear()
    db.flush()
    for course_id in dict.fromkeys(course_ids):
        if db.get(Course, course_id) is None:
            raise HTTPException(status_code=404, detail=f"课程 {course_id} 不存在")
        db.add(CourseRubric(course_id=course_id, rubric_id=rubric.id))
    db.flush()


@router.get("/rubrics", response_model=Page[RubricOut], summary="评分细则列表（支持分页）")
def list_rubrics(
    kind: GradingKind | None = Query(None, description="按作业 / 实验报告筛选"),
    course_id: int | None = Query(None, description="只看挂在该课程下的评分细则"),
    q: str | None = Query(None, description="按名称模糊搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[RubricOut]:
    stmt = select(Rubric).options(*_RUBRIC_LOAD)
    if kind is not None:
        stmt = stmt.where(Rubric.kind == kind)
    if course_id is not None:
        stmt = stmt.join(CourseRubric, CourseRubric.rubric_id == Rubric.id).where(
            CourseRubric.course_id == course_id
        )
    if q:
        stmt = stmt.where(Rubric.name.like(f"%{q}%"))
    stmt = stmt.order_by(Rubric.created_at.desc())
    rows, total = fetch_page(db, stmt, params)
    return make_page([rubric_out(db, item) for item in rows], total, params)


@router.post("/rubrics", response_model=RubricOut, status_code=201, summary="新建评分细则")
def create_rubric(payload: RubricCreate, db: Session = Depends(get_db)) -> RubricOut:
    data = payload.model_dump(exclude={"course_ids"})
    obj = Rubric(**data)
    db.add(obj)
    db.flush()
    if payload.course_ids:
        _sync_courses(db, obj, payload.course_ids)
    db.commit()
    db.refresh(obj)
    return rubric_out(db, obj)


@router.get("/rubrics/{rubric_id}", response_model=RubricOut, summary="评分细则详情")
def get_rubric(rubric_id: int, db: Session = Depends(get_db)) -> RubricOut:
    return rubric_out(db, get_rubric_or_404(db, rubric_id))


@router.patch("/rubrics/{rubric_id}", response_model=RubricOut, summary="修改评分细则")
def update_rubric(
    rubric_id: int, payload: RubricUpdate, db: Session = Depends(get_db)
) -> RubricOut:
    obj = get_rubric_or_404(db, rubric_id)
    data = payload.model_dump(exclude_unset=True, exclude={"course_ids"})
    for field, value in data.items():
        setattr(obj, field, value)
    if payload.course_ids is not None:
        _sync_courses(db, obj, payload.course_ids)
    db.commit()
    db.refresh(obj)
    return rubric_out(db, obj)


@router.post(
    "/rubrics/{rubric_id}/materials/{role}",
    response_model=RubricOut,
    summary="上传或替换评分标准 Word / 原题与答案 PDF",
)
async def upload_rubric_material(
    rubric_id: int,
    role: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> RubricOut:
    if role not in ATTACHMENT_ROLES:
        raise HTTPException(status_code=400, detail="材料类型不正确")
    rubric = get_rubric_or_404(db, rubric_id)
    data = await file.read()
    try:
        _, old_keys = prepare_attachment(
            db,
            rubric,
            role=role,
            filename=file.filename or role,
            data=data,
            content_type=file.content_type,
        )
        db.commit()
    except RubricMaterialError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    delete_object_keys(old_keys)
    db.refresh(rubric)
    return rubric_out(db, rubric)


@router.get(
    "/rubrics/{rubric_id}/materials/{role}/preview",
    response_model=RubricMaterialPreviewOut,
    summary="预览评分细则材料",
)
def preview_rubric_material(
    rubric_id: int, role: str, db: Session = Depends(get_db)
) -> RubricMaterialPreviewOut:
    rubric = get_rubric_or_404(db, rubric_id)
    row = next((item for item in rubric.attachments if item.role == role), None)
    if row is None:
        raise HTTPException(status_code=404, detail="材料不存在")
    return RubricMaterialPreviewOut(
        role=row.role,
        filename=row.filename,
        content_type=row.content_type,
        object_key=row.object_key,
        extracted_text=row.extracted_text,
        page_keys=page_keys(row),
    )


@router.delete(
    "/rubrics/{rubric_id}/materials/{role}",
    status_code=204,
    summary="删除评分细则材料",
)
def delete_rubric_material(
    rubric_id: int, role: str, db: Session = Depends(get_db)
) -> Response:
    rubric = get_rubric_or_404(db, rubric_id)
    row = next((item for item in rubric.attachments if item.role == role), None)
    if row is None:
        raise HTTPException(status_code=404, detail="材料不存在")
    db.delete(row)
    db.commit()
    delete_files(row)
    return Response(status_code=204)


@router.delete(
    "/rubrics/{rubric_id}", status_code=204, summary="删除未被作业使用的评分细则"
)
def delete_rubric(rubric_id: int, db: Session = Depends(get_db)) -> Response:
    obj = get_rubric_or_404(db, rubric_id)
    linked_work = db.execute(
        select(IstudyWork.id).where(IstudyWork.rubric_id == rubric_id).limit(1)
    ).first()
    linked_lab = db.execute(
        select(IstudyLabReport.id).where(IstudyLabReport.rubric_id == rubric_id).limit(1)
    ).first()
    has_results = db.execute(
        select(GradingResult.id).where(GradingResult.rubric_id == rubric_id).limit(1)
    ).first()
    if linked_work or linked_lab or has_results:
        raise HTTPException(
            status_code=409,
            detail="这份评分细则仍被作业使用或已有评分结果，已阻止删除以免丢失数据",
        )
    materials = list(obj.attachments)
    db.delete(obj)
    db.commit()
    for material in materials:
        delete_files(material)
    return Response(status_code=204)


@router.get(
    "/rubrics/{rubric_id}/classes",
    response_model=Page[dict],
    summary="该细则下已有结果的班级（支持分页）",
)
def rubric_classes(
    rubric_id: int,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[dict]:
    get_rubric_or_404(db, rubric_id)
    counts = (
        select(
            GradingResult.class_id.label("class_id"),
            func.count(GradingResult.id).label("result_count"),
        )
        .where(GradingResult.rubric_id == rubric_id)
        .group_by(GradingResult.class_id)
        .subquery()
    )
    stmt = (
        select(
            Class.id,
            Class.department,
            Class.major,
            Class.name,
            counts.c.result_count,
        )
        .join(counts, counts.c.class_id == Class.id)
        .order_by(Class.department, Class.major, Class.name)
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.offset(params.offset).limit(params.page_size)).all()
    items = [
        {
            "class_id": row[0],
            "department": row[1],
            "major": row[2],
            "name": row[3],
            "full_name": "-".join(part for part in (row[1], row[2], row[3]) if part),
            "result_count": row[4],
        }
        for row in rows
    ]
    return make_page(items, total, params)
