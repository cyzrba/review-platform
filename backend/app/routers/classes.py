"""班级 / 学生 / 名单导入。

班级挂在课程下（班级本身是全局的，可以同时挂多个课程）。
导入名单时必须先选课程，导入过程中新建或复用的班级会自动挂到该课程。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import Class, Course, CourseClass, GradingResult, Student
from ..pagination import Page, PageParams, fetch_page, make_page, page_params
from ..schemas import (
    ClassCreate,
    ClassOut,
    ClassUpdate,
    ImportResult,
    StudentCreate,
    StudentOut,
    StudentUpdate,
)
from ..serializers import class_out, student_out
from ..services.importer import ROSTER_TEMPLATE_CSV, ImportError_, parse_roster

router = APIRouter(tags=["班级与学生"])

_CLASS_LOAD = (
    selectinload(Class.students),
    selectinload(Class.course_links).selectinload(CourseClass.course),
)


def _find_class(db: Session, department: str, major: str, name: str) -> Class | None:
    return db.execute(
        select(Class).where(
            Class.department == department, Class.major == major, Class.name == name
        )
    ).scalars().first()


def _link_class_to_course(db: Session, course_id: int, class_id: int) -> bool:
    """把班级挂到课程下；已经挂过就返回 False。"""
    exists = db.execute(
        select(CourseClass.id).where(
            CourseClass.course_id == course_id, CourseClass.class_id == class_id
        )
    ).scalars().first()
    if exists is not None:
        return False
    db.add(CourseClass(course_id=course_id, class_id=class_id))
    return True


# --------------------------------------------------------------------------- #
# 名单导入（放在 /classes/{class_id} 之前，避免路由歧义）
# --------------------------------------------------------------------------- #
@router.get("/classes/import/template", summary="下载名单模板 CSV")
def roster_template() -> Response:
    return Response(
        content=ROSTER_TEMPLATE_CSV.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="roster_template.csv"'},
    )


@router.post("/classes/import", response_model=ImportResult, summary="一键导入班级与学生")
async def import_roster(
    file: UploadFile = File(..., description="CSV 或 XLSX 名单"),
    course_id: int = Form(..., description="导入到哪个课程下（必填）"),
    db: Session = Depends(get_db),
) -> ImportResult:
    """名单列：学号(工号)、姓名、院系、专业、班级、加入时间、入学年份。

    班级按「院系 + 专业 + 班级名称」复用或新建，并自动挂到指定课程下；
    学号相同的记录做更新。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在，请先创建课程")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件是空的")

    try:
        parsed = parse_roster(file.filename or "roster.csv", data)
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = ImportResult()
    class_cache: dict[tuple[str, str, str], Class] = {}
    linked = 0

    for row in parsed.rows:
        department = row.get("department") or ""
        major = row.get("major") or ""
        class_name = row.get("class_name") or ""
        if not class_name:
            result.skipped.append(
                {"student_no": row["student_no"], "reason": "名单里缺少班级名称"}
            )
            continue

        key = (department, major, class_name)
        klass = class_cache.get(key)
        if klass is None:
            klass = _find_class(db, department, major, class_name)
            if klass is None:
                klass = Class(name=class_name, department=department, major=major)
                db.add(klass)
                db.flush()
                result.classes_created += 1
            if _link_class_to_course(db, course.id, klass.id):
                linked += 1
            class_cache[key] = klass

        existing = db.execute(
            select(Student).where(
                Student.class_id == klass.id, Student.student_no == row["student_no"]
            )
        ).scalars().first()

        payload = {
            "name": row["name"],
            "joined_at": row.get("joined_at"),
            "enrollment_year": row.get("enrollment_year"),
            "email": row.get("email"),
        }
        if existing is None:
            db.add(Student(class_id=klass.id, student_no=row["student_no"], **payload))
            result.students_created += 1
            continue

        changed = False
        for field, value in payload.items():
            if value is not None and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if changed:
            result.students_updated += 1

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"导入冲突：{exc.orig}") from exc

    result.message = (
        f"课程「{course.name}」下：新增班级 {result.classes_created} 个（新挂 {linked} 个），"
        f"新增学生 {result.students_created} 人，更新 {result.students_updated} 人，"
        f"跳过 {len(result.skipped) + len(parsed.warnings)} 行"
    )
    result.skipped.extend({"reason": warning} for warning in parsed.warnings)
    return result


# --------------------------------------------------------------------------- #
# 班级
# --------------------------------------------------------------------------- #
@router.get("/classes", response_model=Page[ClassOut], summary="班级列表（支持分页）")
def list_classes(
    q: str | None = Query(None, description="按院系 / 专业 / 班级名搜索"),
    course_id: int | None = Query(None, description="只看挂在该课程下的班级"),
    unlinked_course_id: int | None = Query(
        None, description="只看还没挂到该课程下的班级（用于「挂载已有班级」）"
    ),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[ClassOut]:
    stmt = select(Class).options(*_CLASS_LOAD)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Class.name.like(like), Class.department.like(like), Class.major.like(like))
        )
    if course_id is not None:
        stmt = stmt.join(CourseClass, CourseClass.class_id == Class.id).where(
            CourseClass.course_id == course_id
        )
    if unlinked_course_id is not None:
        linked = select(CourseClass.class_id).where(CourseClass.course_id == unlinked_course_id)
        stmt = stmt.where(not_(Class.id.in_(linked)))
    stmt = stmt.order_by(Class.department, Class.major, Class.name)
    rows, total = fetch_page(db, stmt, params)
    return make_page([class_out(item) for item in rows], total, params)


@router.post("/classes", response_model=ClassOut, status_code=201, summary="新建班级")
def create_class(payload: ClassCreate, db: Session = Depends(get_db)) -> ClassOut:
    if _find_class(db, payload.department, payload.major, payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"班级「{payload.name}」已存在")
    obj = Class(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return class_out(obj, student_count=0)


@router.get("/classes/{class_id}", response_model=ClassOut, summary="班级详情")
def get_class(class_id: int, db: Session = Depends(get_db)) -> ClassOut:
    obj = db.get(Class, class_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    return class_out(obj)


@router.patch("/classes/{class_id}", response_model=ClassOut, summary="修改班级")
def update_class(class_id: int, payload: ClassUpdate, db: Session = Depends(get_db)) -> ClassOut:
    obj = db.get(Class, class_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    db.commit()
    db.refresh(obj)
    return class_out(obj)


@router.delete("/classes/{class_id}", status_code=204, summary="删除班级（连同学生与评分结果）")
def delete_class(class_id: int, db: Session = Depends(get_db)) -> Response:
    obj = db.get(Class, class_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    db.delete(obj)
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# 学生
# --------------------------------------------------------------------------- #
@router.get("/classes/{class_id}/students", response_model=Page[StudentOut], summary="班级学生名单")
def list_class_students(
    class_id: int,
    q: str | None = Query(None, description="按学号 / 姓名搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[StudentOut]:
    if db.get(Class, class_id) is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    stmt = (
        select(Student)
        .options(selectinload(Student.class_))
        .where(Student.class_id == class_id)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Student.student_no.like(like), Student.name.like(like)))
    stmt = stmt.order_by(Student.student_no)
    rows, total = fetch_page(db, stmt, params)
    return make_page([student_out(item) for item in rows], total, params)


@router.get("/students", response_model=Page[StudentOut], summary="学生列表（支持分页）")
def list_students(
    class_id: int | None = None,
    course_id: int | None = Query(None, description="按课程过滤（课程下所有班级的学生）"),
    q: str | None = Query(None, description="学号或姓名模糊搜索"),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
) -> Page[StudentOut]:
    stmt = select(Student).options(selectinload(Student.class_))
    if class_id is not None:
        stmt = stmt.where(Student.class_id == class_id)
    if course_id is not None:
        stmt = stmt.join(CourseClass, CourseClass.class_id == Student.class_id).where(
            CourseClass.course_id == course_id
        )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Student.student_no.like(like), Student.name.like(like)))
    stmt = stmt.order_by(Student.student_no)
    rows, total = fetch_page(db, stmt, params)
    return make_page([student_out(item) for item in rows], total, params)


@router.post("/students", response_model=StudentOut, status_code=201, summary="新增学生")
def create_student(payload: StudentCreate, db: Session = Depends(get_db)) -> StudentOut:
    if db.get(Class, payload.class_id) is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    exists = db.execute(
        select(Student).where(
            Student.class_id == payload.class_id, Student.student_no == payload.student_no
        )
    ).scalars().first()
    if exists:
        raise HTTPException(status_code=409, detail=f"学号 {payload.student_no} 在该班级已存在")
    obj = Student(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return student_out(obj)


@router.patch("/students/{student_id}", response_model=StudentOut, summary="修改学生")
def update_student(
    student_id: int, payload: StudentUpdate, db: Session = Depends(get_db)
) -> StudentOut:
    obj = db.get(Student, student_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="学生不存在")
    data = payload.model_dump(exclude_unset=True)
    if "class_id" in data and db.get(Class, data["class_id"]) is None:
        raise HTTPException(status_code=404, detail="目标班级不存在")
    for field, value in data.items():
        setattr(obj, field, value)
    db.commit()
    db.refresh(obj)
    return student_out(obj)


@router.delete("/students/{student_id}", status_code=204, summary="删除学生")
def delete_student(student_id: int, db: Session = Depends(get_db)) -> Response:
    obj = db.get(Student, student_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="学生不存在")
    db.delete(obj)
    db.commit()
    return Response(status_code=204)


@router.get("/classes/{class_id}/overview", summary="班级概览")
def class_overview(class_id: int, db: Session = Depends(get_db)) -> dict:
    obj = db.get(Class, class_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    student_count = db.execute(
        select(func.count(Student.id)).where(Student.class_id == class_id)
    ).scalar_one()
    result_count = db.execute(
        select(func.count(GradingResult.id)).where(GradingResult.class_id == class_id)
    ).scalar_one()
    return {
        "class_id": class_id,
        "full_name": obj.full_name,
        "student_count": student_count,
        "result_count": result_count,
        "courses": [{"id": link.course.id, "name": link.course.name} for link in obj.course_links],
    }
