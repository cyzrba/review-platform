"""i学习（istudy.szpu.edu.cn）抓取接口。

两块：
  名单  课程列表、连接状态、一键导入学生名单
  作业  同步作业列表、抓某次作业的附件（导出 → 下载 → 抽作答图片 → 匹配学生）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Course,
    ExportStatus,
    GradingResult,
    IstudyExport,
    IstudyLabReport,
    IstudyLabSubmission,
    IstudySubmission,
    IstudyWork,
    ResultStatus,
    Rubric,
    Student,
    WorkStatus,
)
from ..schemas import (
    GradeRunResult,
    IstudyClassCount,
    IstudyCourseOut,
    IstudyExportOut,
    IstudyExportRequest,
    IstudyGradeRequest,
    IstudyLabExportRequest,
    IstudyLabExportResult,
    IstudyLabSyncRequest,
    IstudyLabSyncResult,
    IstudyRosterImportRequest,
    IstudyRosterImportResult,
    IstudyStatus,
    IstudyStudentCompareOut,
    IstudySubmissionOut,
    IstudyWorkCompareOut,
    IstudyWorkGroupOut,
    IstudyWorkListOut,
    IstudyWorkSyncRequest,
    IstudyWorkSyncResult,
)
from ..services import homework_sync, lab_sync
from ..services.grading import enqueue_grading
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


# --------------------------------------------------------------------------- #
# 作业
# --------------------------------------------------------------------------- #
async def _resolve_course_target(
    db: Session, course: Course, cid: str | None, cpi: str | None
) -> tuple[str, str, str]:
    """定出这门本地课程对应 i学习 上的哪门课，返回 (cid, cpi, i学习 课程名)。"""
    if cid and cpi:
        return str(cid), str(cpi), ""
    if course.istudy_cid and course.istudy_cpi:
        return course.istudy_cid, course.istudy_cpi, ""

    async with IstudyClient() as client:
        courses = await client.list_courses()
    matched = next((item for item in courses if item["name"].strip() == course.name.strip()), None)
    if matched is None and len(courses) == 1:
        matched = courses[0]
    if matched is None:
        names = "、".join(item["name"] for item in courses) or "（没有）"
        raise IstudyError(
            f"在 i学习 上找不到课程「{course.name}」，可用课程：{names}。可以手动传 cid / cpi。"
        )
    return matched["cid"], matched["cpi"], matched["name"]


@router.post(
    "/istudy/works/sync",
    response_model=IstudyWorkSyncResult,
    summary="同步作业列表（我教的课 → 课程 → 作业）",
)
async def istudy_sync_works(
    payload: IstudyWorkSyncRequest, db: Session = Depends(get_db)
) -> IstudyWorkSyncResult:
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在，请先创建课程")

    try:
        cid, cpi, source_name = await _resolve_course_target(db, course, payload.cid, payload.cpi)
        scrape = await homework_sync.scrape_course_works(
            cid, cpi, course_name=source_name or course.name
        )
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not scrape.works:
        detail = "没抓到任何作业。"
        if scrape.notes:
            detail += " " + "；".join(scrape.notes[:3])
        raise HTTPException(status_code=400, detail=detail)

    summary = homework_sync.apply_works(db, course, scrape)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"作业列表入库失败：{exc}") from exc

    message = (
        f"课程「{course.name}」：从 i学习 抓到 {len(scrape.works)} 条作业"
        f"（{len({item.name for item in scrape.works})} 次作业 × {len(scrape.class_map)} 个班），"
        f"新增 {summary['created']} 条、更新 {summary['updated']} 条"
    )
    return IstudyWorkSyncResult(
        course_id=course.id,
        course_name=course.name,
        source_course_name=source_name,
        scraped_works=len(scrape.works),
        created=summary["created"],
        updated=summary["updated"],
        class_count=len(scrape.class_map),
        notes=scrape.notes,
        message=message,
    )


@router.get(
    "/istudy/works",
    response_model=IstudyWorkListOut,
    summary="作业列表（按作业名归并，可选按班级 / 状态筛选）",
)
def istudy_list_works(
    course_id: int = Query(..., description="本地课程 id"),
    class_id: int | None = Query(None, description="只看某个本地班级"),
    status: WorkStatus | None = Query(None, description="按作业状态筛选"),
    db: Session = Depends(get_db),
) -> IstudyWorkListOut:
    groups = homework_sync.list_work_groups(
        db, course_id, class_id=class_id, status=status
    )
    synced = [
        item.last_synced_at
        for item in db.execute(
            select(IstudyWork).where(IstudyWork.course_id == course_id)
        )
        .scalars()
        .all()
        if item.last_synced_at
    ]
    return IstudyWorkListOut(
        items=[IstudyWorkGroupOut(**group) for group in groups],
        total=len(groups),
        last_synced_at=max(synced) if synced else None,
    )


@router.post(
    "/istudy/works/{work_id}/export",
    response_model=IstudyExportOut,
    summary="抓这次作业的附件（导出 → 下载 → 抽作答图片 → 匹配学生）",
)
def istudy_export_work(
    work_id: int,
    payload: IstudyExportRequest | None = None,
    db: Session = Depends(get_db),
) -> IstudyExportOut:
    work = db.get(IstudyWork, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="作业不存在，请先同步作业列表")

    request = payload or IstudyExportRequest()
    running = (
        db.execute(
            select(IstudyExport).where(
                IstudyExport.work_id == work.id,
                IstudyExport.status.in_(
                    [
                        ExportStatus.queued,
                        ExportStatus.exporting,
                        ExportStatus.downloading,
                        ExportStatus.parsing,
                    ]
                ),
            )
        )
        .scalars()
        .first()
    )
    if running is not None:
        return IstudyExportOut.model_validate(running)

    row = homework_sync.enqueue_export(
        db, work, content=request.content, fmt=request.fmt, person_ids=request.person_ids
    )
    db.commit()
    homework_sync.enqueue_export_job(row.id)
    return IstudyExportOut.model_validate(row)


@router.get(
    "/istudy/exports/{export_id}",
    response_model=IstudyExportOut,
    summary="导出任务进度",
)
def istudy_export_status(export_id: int, db: Session = Depends(get_db)) -> IstudyExportOut:
    row = db.get(IstudyExport, export_id)
    if row is None:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    return IstudyExportOut.model_validate(row)


@router.get(
    "/istudy/works/{work_id}/submissions",
    response_model=list[IstudySubmissionOut],
    summary="某次作业下每个学生的提交与作答图片",
)
def istudy_work_submissions(
    work_id: int, db: Session = Depends(get_db)
) -> list[IstudySubmissionOut]:
    work = db.get(IstudyWork, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="作业不存在")

    rows = (
        db.execute(
            select(IstudySubmission)
            .where(IstudySubmission.work_id == work.id)
            .order_by(IstudySubmission.student_id)
        )
        .scalars()
        .all()
    )
    student_ids = [row.student_id for row in rows]
    students = {
        item.id: item
        for item in db.execute(select(Student).where(Student.id.in_(student_ids or [-1])))
        .scalars()
        .all()
    }
    out: list[IstudySubmissionOut] = []
    for row in rows:
        student = students.get(row.student_id)
        out.append(
            IstudySubmissionOut(
                id=row.id,
                student_id=row.student_id,
                student_no=student.student_no if student else "",
                student_name=student.name if student else "",
                class_name=work.class_.name if work.class_ else "",
                state=row.state,
                file_state=row.file_state,
                submitted_at=row.submitted_at,
                source_filename=row.source_filename,
                object_key=row.object_key,
                size_bytes=row.size_bytes,
                image_count=row.image_count,
                error_message=row.error_message,
                image_keys=[item.object_key for item in row.files],
            )
        )
    return out


@router.post(
    "/istudy/works/{work_id}/grade",
    response_model=GradeRunResult,
    summary="对这次作业触发 AI 评审（可以在这里换评分细则）",
)
def istudy_grade_work(
    work_id: int,
    payload: IstudyGradeRequest | None = None,
    db: Session = Depends(get_db),
) -> GradeRunResult:
    """评分细则本身只是一份给 AI 看的标准，不跟作业强绑定，所以在这里选。"""
    work = db.get(IstudyWork, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="作业不存在，请先同步作业列表")
    request = payload or IstudyGradeRequest()

    rubric = db.get(Rubric, request.rubric_id) if request.rubric_id else work.rubric
    if rubric is None:
        raise HTTPException(status_code=400, detail="这次作业还没有评分细则，请先选一份")

    switched: dict | None = None
    if work.rubric_id != rubric.id:
        try:
            switched = homework_sync.switch_assignment_rubric(db, work, rubric)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.commit()

    works = homework_sync.assignment_works(db, work)
    class_ids = [item.class_id for item in works if item.class_id]

    stmt = select(GradingResult).where(GradingResult.rubric_id == rubric.id)
    if request.class_id:
        stmt = stmt.where(GradingResult.class_id == request.class_id)
    elif class_ids:
        stmt = stmt.where(GradingResult.class_id.in_(class_ids))
    if not request.force:
        stmt = stmt.where(
            GradingResult.status.in_([ResultStatus.pending, ResultStatus.failed])
        )
    result_ids = [row.id for row in db.execute(stmt).scalars().all()]

    if not result_ids:
        return GradeRunResult(
            queued=0,
            rubric_id=rubric.id,
            message=f"「{work.name}」没有需要评审的记录（可能都评过了，或还没抓附件）",
        )

    enqueue_grading(result_ids)
    message = f"「{work.name}」已提交评审，共 {len(result_ids)} 条，用的细则：{rubric.name}"
    if switched and switched["moved"]:
        message += f"（有 {switched['moved']} 条结果改挂到了这份细则）"
    return GradeRunResult(
        queued=len(result_ids), result_ids=result_ids, rubric_id=rubric.id, message=message
    )


@router.get(
    "/istudy/works/{work_id}/students",
    response_model=IstudyWorkCompareOut,
    summary="对比 i学习 已交名单和本地已抓，找出还没抓的学生",
)
async def istudy_work_students(
    work_id: int, db: Session = Depends(get_db)
) -> IstudyWorkCompareOut:
    work = db.get(IstudyWork, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="作业不存在，请先同步作业列表")
    try:
        data = await homework_sync.compare_work_students(db, work)
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return IstudyWorkCompareOut(
        **{**data, "students": [IstudyStudentCompareOut(**item) for item in data["students"]]}
    )


# --------------------------------------------------------------------------- #
# 实验报告
# --------------------------------------------------------------------------- #
@router.post(
    "/istudy/lab-reports/sync",
    response_model=IstudyLabSyncResult,
    summary="同步实验报告列表（我教的课 → 课程 → 实验报告）",
)
async def istudy_sync_lab_reports(
    payload: IstudyLabSyncRequest, db: Session = Depends(get_db)
) -> IstudyLabSyncResult:
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="课程不存在，请先创建课程")

    try:
        cid, cpi, source_name = await _resolve_course_target(db, course, payload.cid, payload.cpi)
        scrape = await lab_sync.scrape_course_reports(
            cid, cpi, course_name=source_name or course.name
        )
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not scrape.reports:
        detail = "没抓到任何实验报告。"
        if scrape.notes:
            detail += " " + "；".join(scrape.notes[:3])
        raise HTTPException(status_code=400, detail=detail)

    summary = lab_sync.apply_reports(db, course, scrape)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail=f"实验报告入库失败：{exc}") from exc

    names = {item.name for item in scrape.reports}
    class_names = {item.class_name for item in scrape.reports if item.class_name}
    message = (
        f"课程「{course.name}」：从 i学习 抓到 {len(scrape.reports)} 条实验报告"
        f"（{len(names)} 个实验 × {len(class_names)} 个班），"
        f"新增 {summary['created']} 条、更新 {summary['updated']} 条"
    )
    return IstudyLabSyncResult(
        course_id=course.id,
        course_name=course.name,
        source_course_name=source_name,
        scraped_reports=len(scrape.reports),
        experiment_count=len(names),
        created=summary["created"],
        updated=summary["updated"],
        class_count=len(class_names),
        notes=scrape.notes,
        message=message,
    )


@router.get(
    "/istudy/lab-reports",
    response_model=IstudyWorkListOut,
    summary="实验报告列表（按实验名归并，可选按班级 / 状态筛选）",
)
def istudy_list_lab_reports(
    course_id: int = Query(..., description="本地课程 id"),
    class_id: int | None = Query(None, description="只看某个本地班级"),
    status: WorkStatus | None = Query(None, description="按作答状态筛选"),
    db: Session = Depends(get_db),
) -> IstudyWorkListOut:
    groups = lab_sync.list_lab_report_groups(
        db, course_id, class_id=class_id, status=status
    )
    synced = [
        item.last_synced_at
        for item in db.execute(
            select(IstudyLabReport).where(IstudyLabReport.course_id == course_id)
        )
        .scalars()
        .all()
        if item.last_synced_at
    ]
    return IstudyWorkListOut(
        items=[IstudyWorkGroupOut(**group) for group in groups],
        total=len(groups),
        last_synced_at=max(synced) if synced else None,
    )


@router.post(
    "/istudy/lab-reports/{report_id}/export",
    response_model=IstudyLabExportResult,
    summary="抓这次实验报告的作答（下载 → 解包 → 抽作答图片 → 匹配学生）",
)
def istudy_export_lab_report(
    report_id: int,
    payload: IstudyLabExportRequest | None = None,
    db: Session = Depends(get_db),
) -> IstudyLabExportResult:
    report = db.get(IstudyLabReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="实验报告不存在，请先同步实验报告列表")
    if lab_sync.is_export_running(report):
        raise HTTPException(status_code=409, detail="这次实验报告正在抓取中，等它跑完再试")

    request = payload or IstudyLabExportRequest()
    started = lab_sync.start_report_export(
        db, report, student_ids=request.student_ids, force=request.force
    )
    if started["queued"]:
        lab_sync.enqueue_report_export_job(report.id, started["fill_ids"])
    return IstudyLabExportResult(
        report_id=report.id, queued=started["queued"], message=started["message"]
    )


@router.get(
    "/istudy/lab-reports/{report_id}/submissions",
    response_model=list[IstudySubmissionOut],
    summary="某次实验报告下每个学生的提交与作答图片",
)
def istudy_lab_report_submissions(
    report_id: int, db: Session = Depends(get_db)
) -> list[IstudySubmissionOut]:
    report = db.get(IstudyLabReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="实验报告不存在")

    rows = (
        db.execute(
            select(IstudyLabSubmission)
            .where(IstudyLabSubmission.report_id == report.id)
            .order_by(IstudyLabSubmission.student_id)
        )
        .scalars()
        .all()
    )
    student_ids = [row.student_id for row in rows]
    students = {
        item.id: item
        for item in db.execute(select(Student).where(Student.id.in_(student_ids or [-1])))
        .scalars()
        .all()
    }
    class_name = report.class_.name if report.class_ else ""
    out: list[IstudySubmissionOut] = []
    for row in rows:
        student = students.get(row.student_id)
        out.append(
            IstudySubmissionOut(
                id=row.id,
                student_id=row.student_id,
                student_no=student.student_no if student else "",
                student_name=student.name if student else "",
                class_name=class_name,
                state=row.state,
                file_state=row.file_state,
                submitted_at=row.submitted_at,
                source_filename=row.source_filename,
                object_key=row.object_key,
                size_bytes=row.size_bytes,
                image_count=row.image_count,
                error_message=row.error_message,
                image_keys=[item.object_key for item in row.files],
            )
        )
    return out


@router.get(
    "/istudy/lab-reports/{report_id}/students",
    response_model=IstudyWorkCompareOut,
    summary="对比 i学习 已交名单和本地已抓，找出还没抓的学生",
)
async def istudy_lab_report_students(
    report_id: int, db: Session = Depends(get_db)
) -> IstudyWorkCompareOut:
    report = db.get(IstudyLabReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="实验报告不存在，请先同步实验报告列表")
    try:
        data = await lab_sync.compare_report_students(db, report)
    except IstudyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return IstudyWorkCompareOut(
        work_id=data["report_id"],
        work_name=data["report_name"],
        class_id=data["class_id"],
        class_name=data["class_name"],
        istudy_submitted=data["istudy_submitted"],
        local_fetched=data["local_fetched"],
        new_count=data["new_count"],
        students=[IstudyStudentCompareOut(**item) for item in data["students"]],
    )


@router.post(
    "/istudy/lab-reports/{report_id}/grade",
    response_model=GradeRunResult,
    summary="对这次实验报告触发 AI 评审（可以在这里换评分细则）",
)
def istudy_grade_lab_report(
    report_id: int,
    payload: IstudyGradeRequest | None = None,
    db: Session = Depends(get_db),
) -> GradeRunResult:
    report = db.get(IstudyLabReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="实验报告不存在，请先同步实验报告列表")
    request = payload or IstudyGradeRequest()

    rubric = db.get(Rubric, request.rubric_id) if request.rubric_id else report.rubric
    if rubric is None:
        raise HTTPException(status_code=400, detail="这次实验报告还没有评分细则，请先选一份")

    switched: dict | None = None
    if report.rubric_id != rubric.id:
        switched = lab_sync.switch_report_rubric(db, report, rubric)
        db.commit()

    reports = lab_sync.report_group(db, report)
    class_ids = [item.class_id for item in reports if item.class_id]

    stmt = select(GradingResult).where(GradingResult.rubric_id == rubric.id)
    if request.class_id:
        stmt = stmt.where(GradingResult.class_id == request.class_id)
    elif class_ids:
        stmt = stmt.where(GradingResult.class_id.in_(class_ids))
    if not request.force:
        stmt = stmt.where(
            GradingResult.status.in_([ResultStatus.pending, ResultStatus.failed])
        )
    result_ids = [row.id for row in db.execute(stmt).scalars().all()]

    if not result_ids:
        return GradeRunResult(
            queued=0,
            rubric_id=rubric.id,
            message=f"「{report.name}」没有需要评审的记录（可能都评过了，或还没抓作答）",
        )

    enqueue_grading(result_ids)
    message = f"「{report.name}」已提交评审，共 {len(result_ids)} 条，用的细则：{rubric.name}"
    if switched and switched["moved"]:
        message += f"（有 {switched['moved']} 条结果改挂到了这份细则）"
    return GradeRunResult(
        queued=len(result_ids), result_ids=result_ids, rubric_id=rubric.id, message=message
    )
