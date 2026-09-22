"""课程，以及课程与班级 / 评分细则的关联。

课程是最外层的组织单位：一个课程有多个班级（班级也可以同时挂在多个课程下），
一个课程有多份评分细则。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Class, Course, CourseClass, CourseRubric, Rubric
from ..pagination import Page, PageParams, fetch_page, make_page, page_params
from ..schemas import (
    ClassOut,
    CourseCreate,
    CourseOut,
    CourseUpdate,
    LinkClassesRequest,
    LinkResult,
    LinkRubricsRequest,
    RosterClearResult,
    RubricOut,
)
from ..serializers import class_out, course_out, rubric_out
from ..services.roster_sync import clear_course_roster

router = APIRouter(tags=["课程"])

_COURSE_LOAD = (
    selectinload(Course.class_links),
    selectinload(Course.rubric_links),
)


def get_course_or_404(db: Session, course_id: int) -> Course:
    obj = db.get(Course, course_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    return obj


@router.get("/courses", response_model=Page[CourseOut], summary="课程列表")
def list_courses(
    q: str | None = Query(None, description="按课程名 / 课程代码搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[CourseOut]:
    stmt = select(Course).options(*_COURSE_LOAD)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Course.name.like(like), Course.code.like(like)))
    stmt = stmt.order_by(Course.created_at.desc())
    rows, total = fetch_page(db, stmt, params)
    return make_page([course_out(db, item) for item in rows], total, params)


@router.post("/courses", response_model=CourseOut, status_code=201, summary="新建课程")
def create_course(payload: CourseCreate, db: Session = Depends(get_db)) -> CourseOut:
    exists = db.execute(select(Course).where(Course.name == payload.name)).scalars().first()
    if exists:
        raise HTTPException(status_code=409, detail=f"课程「{payload.name}」已存在")
    obj = Course(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return course_out(db, obj)


@router.get("/courses/{course_id}", response_model=CourseOut, summary="课程详情")
def get_course(course_id: int, db: Session = Depends(get_db)) -> CourseOut:
    return course_out(db, get_course_or_404(db, course_id))


@router.patch("/courses/{course_id}", response_model=CourseOut, summary="修改课程")
def update_course(
    course_id: int, payload: CourseUpdate, db: Session = Depends(get_db)
) -> CourseOut:
    obj = get_course_or_404(db, course_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    db.commit()
    db.refresh(obj)
    return course_out(db, obj)


@router.delete(
    "/courses/{course_id}", status_code=204, summary="删除课程（只删关联，不删班级和细则）"
)
def delete_course(course_id: int, db: Session = Depends(get_db)) -> Response:
    obj = get_course_or_404(db, course_id)
    db.delete(obj)
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# 课程 - 班级
# --------------------------------------------------------------------------- #
@router.get(
    "/courses/{course_id}/classes", response_model=Page[ClassOut], summary="课程下的班级"
)
def list_course_classes(
    course_id: int,
    q: str | None = Query(None, description="按院系 / 专业 / 班级名搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[ClassOut]:
    get_course_or_404(db, course_id)
    stmt = (
        select(Class)
        .join(CourseClass, CourseClass.class_id == Class.id)
        .options(
            selectinload(Class.students),
            selectinload(Class.course_links).selectinload(CourseClass.course),
        )
        .where(CourseClass.course_id == course_id)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Class.name.like(like), Class.department.like(like), Class.major.like(like))
        )
    stmt = stmt.order_by(Class.department, Class.major, Class.name)
    rows, total = fetch_page(db, stmt, params)
    return make_page([class_out(item) for item in rows], total, params)


@router.post("/courses/{course_id}/classes", response_model=LinkResult, summary="把班级挂到课程")
def link_classes(
    course_id: int, payload: LinkClassesRequest, db: Session = Depends(get_db)
) -> LinkResult:
    get_course_or_404(db, course_id)
    existing = set(
        db.execute(
            select(CourseClass.class_id).where(CourseClass.course_id == course_id)
        ).scalars().all()
    )
    linked = skipped = 0
    for class_id in dict.fromkeys(payload.class_ids):
        if class_id in existing:
            skipped += 1
            continue
        if db.get(Class, class_id) is None:
            skipped += 1
            continue
        db.add(CourseClass(course_id=course_id, class_id=class_id))
        linked += 1
    db.commit()
    return LinkResult(
        course_id=course_id,
        linked=linked,
        skipped=skipped,
        message=f"新增关联 {linked} 个班级，跳过 {skipped} 个",
    )


@router.delete(
    "/courses/{course_id}/classes/{class_id}",
    status_code=204,
    summary="把班级从课程里摘掉（不影响班级本身）",
)
def unlink_class(course_id: int, class_id: int, db: Session = Depends(get_db)) -> Response:
    link = db.execute(
        select(CourseClass).where(
            CourseClass.course_id == course_id, CourseClass.class_id == class_id
        )
    ).scalars().first()
    if link is None:
        raise HTTPException(status_code=404, detail="该班级没有挂在这个课程下")
    db.delete(link)
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# 课程 - 评分细则
# --------------------------------------------------------------------------- #
@router.get(
    "/courses/{course_id}/rubrics", response_model=Page[RubricOut], summary="课程下的评分细则"
)
def list_course_rubrics(
    course_id: int,
    kind: str | None = Query(None, description="homework / lab_report"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[RubricOut]:
    get_course_or_404(db, course_id)
    stmt = (
        select(Rubric)
        .join(CourseRubric, CourseRubric.rubric_id == Rubric.id)
        .options(selectinload(Rubric.course_links).selectinload(CourseRubric.course))
        .where(CourseRubric.course_id == course_id)
    )
    if kind:
        stmt = stmt.where(Rubric.kind == kind)
    stmt = stmt.order_by(Rubric.created_at.desc())
    rows, total = fetch_page(db, stmt, params)
    return make_page([rubric_out(db, item) for item in rows], total, params)


@router.post(
    "/courses/{course_id}/rubrics", response_model=LinkResult, summary="把评分细则挂到课程"
)
def link_rubrics(
    course_id: int, payload: LinkRubricsRequest, db: Session = Depends(get_db)
) -> LinkResult:
    get_course_or_404(db, course_id)
    existing = set(
        db.execute(
            select(CourseRubric.rubric_id).where(CourseRubric.course_id == course_id)
        ).scalars().all()
    )
    linked = skipped = 0
    for rubric_id in dict.fromkeys(payload.rubric_ids):
        if rubric_id in existing:
            skipped += 1
            continue
        if db.get(Rubric, rubric_id) is None:
            skipped += 1
            continue
        db.add(CourseRubric(course_id=course_id, rubric_id=rubric_id))
        linked += 1
    db.commit()
    return LinkResult(
        course_id=course_id,
        linked=linked,
        skipped=skipped,
        message=f"新增关联 {linked} 份评分细则，跳过 {skipped} 份",
    )


@router.delete(
    "/courses/{course_id}/rubrics/{rubric_id}",
    status_code=204,
    summary="把评分细则从课程里摘掉（不影响细则本身）",
)
def unlink_rubric(course_id: int, rubric_id: int, db: Session = Depends(get_db)) -> Response:
    link = db.execute(
        select(CourseRubric).where(
            CourseRubric.course_id == course_id, CourseRubric.rubric_id == rubric_id
        )
    ).scalars().first()
    if link is None:
        raise HTTPException(status_code=404, detail="该评分细则没有挂在这个课程下")
    db.delete(link)
    db.commit()
    return Response(status_code=204)


@router.delete(
    "/courses/{course_id}/roster",
    response_model=RosterClearResult,
    summary="清空课程名单（班级、学生、评分结果）",
)
def clear_roster(course_id: int, db: Session = Depends(get_db)) -> RosterClearResult:
    """把该课程名下的班级、学生以及关联的评分结果全部清掉。

    只挂在别的课程下的班级不受影响；同一个班级还挂在其它课程时只摘掉关联。
    """
    course = get_course_or_404(db, course_id)
    summary = clear_course_roster(db, course)
    db.commit()
    return RosterClearResult(
        course_id=course.id,
        course_name=course.name,
        classes_removed=summary.classes_removed,
        students_removed=summary.students_removed,
        results_removed=summary.results_removed,
        message=(
            f"课程「{course.name}」：已清空 {summary.classes_removed} 个班级、"
            f"{summary.students_removed} 名学生、{summary.results_removed} 条评分结果"
        ),
    )
