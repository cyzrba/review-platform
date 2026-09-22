"""Pydantic 请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import GradingKind, ResultStatus


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
    is_manual: bool = False
    error_message: str | None = None
    created_at: datetime


class GradingResultUpdate(BaseModel):
    """教师人工校正某条评分结果。"""

    score: float | None = None
    comment: str | None = None


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
