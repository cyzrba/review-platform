"""Pydantic 请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import ExportStatus, FileState, GradingKind, ResultStatus, SubmissionState, WorkStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# 课程
# --------------------------------------------------------------------------- #
class CourseBrief(BaseModel):
    id: int
    name: str


class CourseBase(BaseModel):
    name: str = Field(..., max_length=200, description="课程名称，唯一")
    code: str | None = Field(None, max_length=64, description="课程代码")
    term: str | None = Field(None, max_length=64, description="学期，如 2024-2025-1")
    description: str | None = None


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    term: str | None = None
    description: str | None = None


class CourseOut(ORMModel, CourseBase):
    id: int
    created_at: datetime
    class_count: int = 0
    student_count: int = 0
    rubric_count: int = 0


class LinkClassesRequest(BaseModel):
    class_ids: list[int] = Field(..., min_length=1, description="要挂到该课程下的班级 id")


class LinkRubricsRequest(BaseModel):
    rubric_ids: list[int] = Field(..., min_length=1, description="要挂到该课程下的评分细则 id")


class LinkResult(BaseModel):
    course_id: int
    linked: int = 0
    skipped: int = 0
    message: str = ""


# --------------------------------------------------------------------------- #
# 班级 / 学生
# --------------------------------------------------------------------------- #
class ClassBase(BaseModel):
    name: str = Field(..., max_length=128, description="班级名称")
    department: str = Field("", max_length=128, description="院系")
    major: str = Field("", max_length=128, description="专业")
    description: str | None = None


class ClassCreate(ClassBase):
    pass


class ClassUpdate(BaseModel):
    name: str | None = None
    department: str | None = None
    major: str | None = None
    description: str | None = None


class ClassOut(ORMModel, ClassBase):
    id: int
    full_name: str = ""
    student_count: int = 0
    courses: list[CourseBrief] = Field(default_factory=list, description="挂靠的课程")
    created_at: datetime


class StudentBase(BaseModel):
    student_no: str = Field(..., max_length=64, description="学号 / 工号")
    name: str = Field(..., max_length=64, description="姓名")
    joined_at: datetime | None = Field(None, description="加入时间")
    enrollment_year: int | None = Field(None, ge=1900, le=2200, description="入学年份")
    email: str | None = None
    remark: str | None = None


class StudentCreate(StudentBase):
    class_id: int


class StudentUpdate(BaseModel):
    student_no: str | None = None
    name: str | None = None
    joined_at: datetime | None = None
    enrollment_year: int | None = None
    email: str | None = None
    remark: str | None = None
    class_id: int | None = None


class StudentOut(ORMModel, StudentBase):
    id: int
    class_id: int
    class_name: str | None = None
    department: str | None = None
    major: str | None = None
    created_at: datetime


class ImportResult(BaseModel):
    classes_created: int = 0
    students_created: int = 0
    students_updated: int = 0
    skipped: list[dict] = Field(default_factory=list, description="被跳过的行及原因")
    message: str = ""


# --------------------------------------------------------------------------- #
# 评分细则
# --------------------------------------------------------------------------- #
class RubricBase(BaseModel):
    name: str = Field(..., max_length=200, description="项目 / 作业名称")
    kind: GradingKind = GradingKind.homework
    description: str | None = Field(None, description="任务说明 / 题目要求")
    criteria: str | None = Field(None, description="评分细则正文，作为 AI 评审依据")
    total_score: float = Field(100.0, gt=0, description="满分")
    extra_prompt: str | None = Field(None, description="附加给 AI 的提示词")


class RubricCreate(RubricBase):
    course_ids: list[int] = Field(default_factory=list, description="挂到哪些课程下，可选")


class RubricUpdate(BaseModel):
    name: str | None = None
    kind: GradingKind | None = None
    description: str | None = None
    criteria: str | None = None
    total_score: float | None = None
    extra_prompt: str | None = None
    course_ids: list[int] | None = Field(
        None, description="传了就整体替换该细则的课程关联，不传则不动"
    )


class RubricStats(BaseModel):
    result_count: int = 0
    graded_count: int = 0
    pending_count: int = 0
    failed_count: int = 0
    class_count: int = 0
    average_score: float | None = None
    max_score: float | None = None
    min_score: float | None = None


class RubricOut(ORMModel, RubricBase):
    id: int
    created_at: datetime
    stats: RubricStats = Field(default_factory=RubricStats)
    courses: list[CourseBrief] = Field(default_factory=list, description="挂靠的课程")


# --------------------------------------------------------------------------- #
# 评分结果
# --------------------------------------------------------------------------- #
class GradingResultOut(ORMModel):
    id: int
    rubric_id: int
    rubric_name: str | None = None
    student_id: int
    student_no: str | None = None
    student_name: str | None = None
    class_id: int
    class_name: str | None = None
    department: str | None = None
    major: str | None = None
    source_filename: str
    object_key: str
    size_bytes: int
    match_reason: str | None = None
    status: ResultStatus
    score: float | None = None
    comment: str | None = None
    model: str | None = None
    graded_at: datetime | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    images_sent: int | None = None
    is_manual: bool = False
    error_message: str | None = None
    created_at: datetime


class GradingResultUpdate(BaseModel):
    """教师人工校正某条评分结果。"""

    score: float | None = None
    comment: str | None = None


class GradingResultImageOut(BaseModel):
    """复核时给学生看的图：题面 + 他自己的作答。"""

    role: str = Field("answer", description="question 题面 / answer 作答")
    filename: str
    object_key: str
    content_type: str | None = None
    size_bytes: int = 0


class UploadIssueOut(BaseModel):
    """解析压缩包时被跳过或没能匹配上的文件（只在响应里返回，不入库）。"""

    filename: str
    reason: str


class UploadSummary(BaseModel):
    rubric_id: int
    kind: GradingKind
    scanned_files: int = Field(0, description="展开压缩包后扫到的文件总数")
    accepted_files: int = Field(0, description="符合类型要求、参与匹配的文件数")
    matched: int = 0
    created: int = 0
    updated: int = 0
    issues: list[UploadIssueOut] = Field(default_factory=list)
    message: str = ""


class GradeRunRequest(BaseModel):
    result_ids: list[int] | None = Field(None, description="留空表示该细则下所有待评审记录")
    force: bool = Field(False, description="已评审的也重新评一遍")
    class_id: int | None = Field(None, description="只评审某个班级")
    course_id: int | None = Field(None, description="只评审某个课程下班级的记录")


class GradeRunResult(BaseModel):
    queued: int
    result_ids: list[int] = Field(default_factory=list)
    rubric_id: int | None = Field(None, description="这次评审实际用的评分细则")
    message: str = ""


# --------------------------------------------------------------------------- #
# 系统
# --------------------------------------------------------------------------- #
class FileUrlOut(BaseModel):
    object_key: str
    url: str
    expires_in: int | None = None


class StorageHealth(BaseModel):
    backend: str
    endpoint: str | None = None
    bucket: str
    ok: bool
    detail: str | None = None


class DashboardOut(BaseModel):
    course_count: int
    class_count: int
    student_count: int
    rubric_count: int
    result_count: int
    graded_count: int
    pending_count: int
    storage: StorageHealth


# --------------------------------------------------------------------------- #
# i学习 抓取
# --------------------------------------------------------------------------- #
class IstudyStatus(BaseModel):
    """i学习 连接状态：浏览器调试端口是否可用、登录态是否有效。"""

    available: bool = Field(False, description="能否连上浏览器调试端口")
    logged_in: bool = Field(False, description="i学习 登录态是否有效")
    browser: str | None = Field(None, description="浏览器标识")
    message: str = ""


class IstudyCourseOut(BaseModel):
    """i学习 上的课程。"""

    cid: str
    cpi: str
    name: str


class IstudyRosterImportRequest(BaseModel):
    course_id: int = Field(..., description="导入到本地哪个课程下")
    cid: str | None = Field(None, description="i学习 课程 id；不传则按课程名自动匹配")
    cpi: str | None = Field(None, description="i学习 课程 cpi")
    source_course_name: str | None = Field(
        None, description="i学习 课程名，用于自动匹配本地课程"
    )
    replace: bool = Field(
        False, description="导入前是否清空该课程现有名单（班级 / 学生 / 评分结果）"
    )


class IstudyClassCount(BaseModel):
    class_name: str
    count: int = 0
    error: str | None = None


class IstudyRosterImportResult(BaseModel):
    course_id: int
    course_name: str
    source_course_name: str
    classes: list[IstudyClassCount] = Field(default_factory=list)
    scraped_students: int = Field(0, description="抓到的学生条数")
    classes_created: int = 0
    classes_linked: int = 0
    students_created: int = 0
    students_updated: int = 0
    classes_removed: int = 0
    students_removed: int = 0
    results_removed: int = 0
    skipped: list[dict] = Field(default_factory=list)
    message: str = ""


class RosterClearResult(BaseModel):
    course_id: int
    course_name: str
    classes_removed: int = 0
    students_removed: int = 0
    results_removed: int = 0
    message: str = ""


# --------------------------------------------------------------------------- #
# i学习 作业
# --------------------------------------------------------------------------- #
class IstudyWorkSyncRequest(BaseModel):
    course_id: int = Field(..., description="同步到本地哪个课程下")
    cid: str | None = Field(None, description="i学习 课程 id；不传则用课程已绑定的")
    cpi: str | None = Field(None, description="i学习 课程 cpi")


class IstudyExportOut(ORMModel):
    id: int
    work_id: int
    content: int = 0
    fmt: int = 1
    status: ExportStatus = ExportStatus.queued
    istudy_download_id: str | None = None
    size_bytes: int = 0
    file_count: int = 0
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class IstudyWorkClassOut(BaseModel):
    """作业在某个班下的实例。"""

    source: str = Field("work", description="work=作业 / lab=实验报告")
    work_id: int = Field(..., description="本地 istudy_works.id")
    class_id: int | None = None
    class_name: str = ""
    rubric_id: int | None = None
    istudy_clazzid: str = ""
    istudy_work_id: str = ""
    report_type: int | None = Field(None, description="实验报告：1 表单 / 2 附件 / 3 Word 模板")
    status: WorkStatus = WorkStatus.ongoing
    start_at: datetime | None = None
    end_at: datetime | None = None
    submitted_count: int = 0
    unsubmitted_count: int = 0
    pending_count: int = 0
    captured_count: int = Field(0, description="本地已经抓到文件的人数")
    missing_count: int = Field(0, description="本地判定为未交的人数")
    image_count: int = Field(0, description="抓到的作答图片数")
    result_count: int = Field(0, description="这个班已有的评分结果")
    graded_count: int = Field(0, description="这个班已评审条数")
    last_export: IstudyExportOut | None = None
    sync_state: str | None = Field(None, description="实验报告：抓附件的进度")
    sync_message: str | None = Field(None, description="实验报告：抓附件的说明")


class IstudyWorkGroupOut(BaseModel):
    """列表里的一行：一次作业（可能发给了多个班）。"""

    source: str = Field("work", description="work=作业 / lab=实验报告")
    name: str
    library_id: str | None = Field(None, description="i学习 作业题库 id，一次作业的唯一标识")
    task_ids: list[str] = Field(default_factory=list)
    rubric_ids: list[int] = Field(default_factory=list, description="这一行关联到的评分细则")
    status: WorkStatus | None = None
    status_breakdown: dict[str, int] = Field(default_factory=dict)
    start_at: datetime | None = None
    end_at: datetime | None = None
    class_count: int = 0
    rubric_id: int | None = None
    submitted_count: int = 0
    unsubmitted_count: int = 0
    pending_count: int = 0
    captured_count: int = 0
    missing_count: int = 0
    image_count: int = 0
    result_count: int = 0
    graded_count: int = 0
    classes: list[IstudyWorkClassOut] = Field(default_factory=list)


class IstudyWorkListOut(BaseModel):
    items: list[IstudyWorkGroupOut] = Field(default_factory=list)
    total: int = 0
    last_synced_at: datetime | None = None


class IstudyWorkSyncResult(BaseModel):
    course_id: int
    course_name: str
    source_course_name: str = ""
    scraped_works: int = 0
    created: int = 0
    updated: int = 0
    class_count: int = 0
    notes: list[str] = Field(default_factory=list)
    message: str = ""


class IstudyExportRequest(BaseModel):
    content: int = Field(0, ge=0, le=2, description="0 完整答题记录 / 1 仅提交附件 / 2 仅留痕批注")
    fmt: int = Field(1, ge=0, le=1, description="0 Word / 1 PDF")
    person_ids: list[str] | None = Field(
        None, description="只导出这几个学生（i学习 的用户 id）；不传就整班导出"
    )


class IstudyStudentCompareOut(BaseModel):
    """i学习 已交名单 vs 本地已抓，一行一个学生。"""

    student_no: str
    name: str = ""
    istudy_user_id: str | None = Field(None, description="按人导出时要用它")
    answer_id: str | None = None
    submitted_at: str | None = Field(None, description="i学习 上的提交时间")
    state: str = Field(
        "new", description="new 还没抓 / fetched 已抓 / not_submitted 没交 / not_in_roster 名单里没有"
    )
    fetched: bool = False
    in_roster: bool = True
    image_count: int = 0


class IstudyWorkCompareOut(BaseModel):
    work_id: int
    work_name: str
    class_id: int | None = None
    class_name: str = ""
    istudy_submitted: int = Field(0, description="i学习 上已交人数")
    local_fetched: int = Field(0, description="本地已抓人数")
    new_count: int = Field(0, description="还没抓的人数")
    students: list[IstudyStudentCompareOut] = Field(default_factory=list)


class IstudyGradeRequest(BaseModel):
    """「一键 AI 评审」弹窗里选的东西。"""

    rubric_id: int | None = Field(
        None, description="本次评审用哪一份评分细则；不传就用这次作业默认绑定的那份"
    )
    class_id: int | None = Field(None, description="只评审某个班级；不传就是这次作业的所有班")
    force: bool = Field(False, description="已评审的也重新评一遍")


class IstudySubmissionOut(BaseModel):
    id: int
    student_id: int
    student_no: str = ""
    student_name: str = ""
    class_name: str = ""
    state: SubmissionState = SubmissionState.missing
    file_state: FileState = FileState.none
    submitted_at: datetime | None = None
    source_filename: str | None = None
    object_key: str | None = None
    size_bytes: int = 0
    image_count: int = 0
    error_message: str | None = None
    image_keys: list[str] = Field(default_factory=list, description="作答图片的 object key，按顺序")


# --------------------------------------------------------------------------- #
# i学习 实验报告
# --------------------------------------------------------------------------- #
class IstudyLabSyncRequest(BaseModel):
    course_id: int = Field(..., description="同步到本地哪个课程下")
    cid: str | None = Field(None, description="i学习 课程 id；不传则用课程已绑定的")
    cpi: str | None = Field(None, description="i学习 课程 cpi")


class IstudyLabSyncResult(BaseModel):
    course_id: int
    course_name: str
    source_course_name: str = ""
    scraped_reports: int = Field(0, description="抓到的「报告 × 班级」条数")
    experiment_count: int = Field(0, description="去重后的实验个数")
    created: int = 0
    updated: int = 0
    class_count: int = 0
    notes: list[str] = Field(default_factory=list)
    message: str = ""


class IstudyLabExportRequest(BaseModel):
    """抓某次实验报告的作答。"""

    student_ids: list[str] | None = Field(
        None, description="只抓这几个人（i学习 的 fillId）；不传就抓还没抓过的人"
    )
    force: bool = Field(False, description="已经抓过的也重新抓一遍")


class IstudyLabExportResult(BaseModel):
    report_id: int
    queued: int = 0
    message: str = ""
