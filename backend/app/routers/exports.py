"""成绩与评语导出。"""

from __future__ import annotations

import csv
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Class, Course, Rubric
from ..services.exporter import build_workbook, export_filename, load_rows

router = APIRouter(tags=["导出"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _load(
    db: Session,
    rubric_id: int,
    class_id: int | None,
    course_id: int | None,
    include_missing: bool,
):
    rubric = db.get(Rubric, rubric_id)
    if rubric is None:
        raise HTTPException(status_code=404, detail="评分细则不存在")
    klass = db.get(Class, class_id) if class_id is not None else None
    if class_id is not None and klass is None:
        raise HTTPException(status_code=404, detail="班级不存在")
    course = db.get(Course, course_id) if course_id is not None else None
    if course_id is not None and course is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    rows = load_rows(
        db,
        rubric_id,
        class_id=class_id,
        course_id=course_id,
        include_missing=include_missing,
    )
    return rubric, klass, course, rows


def _disposition(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"}


@router.get("/rubrics/{rubric_id}/export.xlsx", summary="一键导出成绩与评语（Excel）")
def export_xlsx(
    rubric_id: int,
    class_id: int | None = Query(None, description="只导出某个班级"),
    course_id: int | None = Query(None, description="只导出某个课程下的班级"),
    include_missing: bool = Query(True, description="是否补上未提交的学生"),
    db: Session = Depends(get_db),
) -> Response:
    rubric, klass, course, rows = _load(db, rubric_id, class_id, course_id, include_missing)
    payload = build_workbook(db, rubric, rows)
    scope = klass.name if klass else (course.name if course else None)
    return Response(
        content=payload,
        media_type=_XLSX_MEDIA,
        headers={
            **_disposition(export_filename(rubric, scope)),
            "Content-Length": str(len(payload)),
        },
    )


@router.get("/rubrics/{rubric_id}/export.csv", summary="一键导出成绩与评语（CSV）")
def export_csv(
    rubric_id: int,
    class_id: int | None = Query(None, description="只导出某个班级"),
    course_id: int | None = Query(None, description="只导出某个课程下的班级"),
    include_missing: bool = Query(True, description="是否补上未提交的学生"),
    db: Session = Depends(get_db),
) -> Response:
    rubric, klass, course, rows = _load(db, rubric_id, class_id, course_id, include_missing)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["院系", "专业", "班级", "学号", "姓名", "提交文件", "状态", "总分", "评语", "评审时间"])
    for row in rows:
        writer.writerow(
            [
                row["department"],
                row["major"],
                row["class_name"],
                row["student_no"],
                row["student_name"],
                row["filename"],
                row["status"],
                row["score"] if row["score"] is not None else "",
                row["comment"],
                row["graded_at"],
            ]
        )

    payload = buffer.getvalue().encode("utf-8-sig")
    scope = klass.name if klass else (course.name if course else None)
    filename = export_filename(rubric, scope).replace(".xlsx", ".csv")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers=_disposition(filename),
    )
