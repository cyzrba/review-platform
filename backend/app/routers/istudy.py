"""i学习（istudy.szpu.edu.cn）抓取接口：课程列表、连接状态、一键导入学生名单。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Course
from ..schemas import (
    IstudyClassCount,
    IstudyCourseOut,
    IstudyRosterImportRequest,
    IstudyRosterImportResult,
    IstudyStatus,
)
from ..services.istudy import IstudyClient, IstudyError
from ..services.roster_sync import import_rows

router = APIRouter(tags=["i学习"])


@router.get("/istudy/status", response_model=IstudyStatus, summary="i学习 连接与登录状态")
async def istudy_status() -> IstudyStatus:
    try:
        async with IstudyClient() as client:
            logged_in = await client.logged_in()
            return IstudyStatus(
                available=True,
                logged_in=logged_in,
                browser=client.browser,
                message="浏览器就绪" + ("，已登录 i学习" if logged_in else "，但 i学习 未登录"),
            )
    except IstudyError as exc:
        return IstudyStatus(available=False, logged_in=False, message=str(exc))


@router.get("/istudy/courses", response_model=list[IstudyCourseOut], summary="i学习 上我教的课")
async def istudy_courses() -> list[IstudyCourseOut]:
    try:
        async with IstudyClient() as client:
            courses = await client.list_courses()
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [IstudyCourseOut(**item) for item in courses]


@router.post(
    "/istudy/roster/import",
    response_model=IstudyRosterImportResult,
    summary="一键导入学生名单（从 i学习 抓取）",
)
async def istudy_import_roster(
    payload: IstudyRosterImportRequest, db: Session = Depends(get_db)
) -> IstudyRosterImportResult:
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在，请先创建课程")

    try:
        async with IstudyClient() as client:
            courses = await client.list_courses()
            if not courses:
                raise IstudyError("i学习 上没有找到你教的课程（或者登录态已失效）")

            cid, cpi, source_name = payload.cid, payload.cpi, payload.source_course_name or ""
            if not (cid and cpi):
                wanted = (payload.source_course_name or course.name).strip()
                matched = next((item for item in courses if item["name"].strip() == wanted), None)
                if matched is None and len(courses) == 1:
                    matched = courses[0]
                if matched is None:
                    names = "、".join(item["name"] for item in courses)
                    raise IstudyError(
                        f"在 i学习 上找不到课程「{wanted}」，可用课程：{names}。"
                        "请指定要抓取的课程（cid/cpi）。"
                    )
                cid, cpi, source_name = matched["cid"], matched["cpi"], matched["name"]

            data = await client.fetch_roster(str(cid), str(cpi))
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rows = data["rows"]
    if not rows:
        raise HTTPException(status_code=400, detail="没抓到任何学生，请检查课程里的班级名单")

    summary = import_rows(db, course, rows, replace=payload.replace)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"导入失败：{exc}") from exc

    classes = [
        IstudyClassCount(
            class_name=item["class_name"], count=item.get("count", 0), error=item.get("error")
        )
        for item in data["classes"]
    ]
    message = (
        f"课程「{course.name}」：从 i学习「{source_name}」抓到 {len(rows)} 名学生，"
        f"涉及 {len(classes)} 个班级；"
        f"新增班级 {summary.classes_created} 个，新增学生 {summary.students_created} 人，"
        f"更新 {summary.students_updated} 人"
    )
    if summary.students_removed or summary.classes_removed:
        message += (
            f"；导入前清空 {summary.classes_removed} 个班级、"
            f"{summary.students_removed} 名学生、{summary.results_removed} 条评分结果"
        )
    return IstudyRosterImportResult(
        course_id=course.id,
        course_name=course.name,
        source_course_name=source_name,
        classes=classes,
        scraped_students=len(rows),
        classes_created=summary.classes_created,
        classes_linked=summary.classes_linked,
        students_created=summary.students_created,
        students_updated=summary.students_updated,
        classes_removed=summary.classes_removed,
        students_removed=summary.students_removed,
        results_removed=summary.results_removed,
        skipped=summary.skipped,
        message=message,
    )
