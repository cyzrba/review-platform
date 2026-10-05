"""数据模型（SQLite 表结构）。

一共 14 张表，前 7 张是平台自己的业务表，后 7 张用来承接「深职 i学习」抓下来的
作业与实验报告：

    courses                  课程
    course_classes           课程-班级关联（一个课程多个班级，一个班级可挂多个课程）
    course_rubrics           课程-评分标准关联（一个课程多份评分细则）
    classes                  班级（院系 + 专业 + 班级名称）
    students                 学生（学号/工号、姓名、加入时间、入学年份）
    rubrics                  评分细则（一个项目 / 一次作业对应一份，不分版本、不分维度）
    grading_results          评分结果（某评分细则下、某班级、某学生的总分与评语）

    istudy_works             i学习 作业 × 班级（一次作业发到多个班，每班一个 workId）
    istudy_submissions       i学习 作业 × 学生（谁交了、整包文件放哪）
    istudy_submission_files  学生提交里拆出来的单个文件（主要是作答图片，直接喂给 AI）
    istudy_exports           i学习 导出任务（导出是异步的，要排队和轮询）

    istudy_lab_reports       i学习 实验报告 × 班级（一次报告发给多个班，每班一行）
    istudy_lab_submissions   i学习 实验报告 × 学生
    istudy_lab_files         实验报告提交里拆出来的作答图片
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _enum_column(enum_cls: type[enum.Enum], length: int = 32):
    """把 Python 枚举按 value 存成字符串，SQLite 上不做原生枚举。"""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class GradingKind(str, enum.Enum):
    homework = "homework"  # 作业
    lab_report = "lab_report"  # 实验报告


class ResultStatus(str, enum.Enum):
    pending = "pending"  # 待评审
    grading = "grading"  # 评审中
    graded = "graded"  # 已评审
    failed = "failed"  # 评审失败


class WorkStatus(str, enum.Enum):
    """i学习 作业的状态，对应列表页 未开始 / 进行中 / 已结束。"""

    not_started = "not_started"  # 未开始
    ongoing = "ongoing"  # 进行中
    ended = "ended"  # 已结束


class SubmissionState(str, enum.Enum):
    """学生在某次作业下的提交状态。"""

    submitted = "submitted"  # 已交
    draft = "draft"  # 已保存（学生存了草稿但没交）
    missing = "missing"  # 未交


class FileState(str, enum.Enum):
    """本地把学生文件搬下来的进度。"""

    none = "none"  # 还没抓
    pending = "pending"  # 抓取中
    ready = "ready"  # 文件已就位
    failed = "failed"  # 抓取失败


class ExportStatus(str, enum.Enum):
    """i学习「导出作业附件」任务的进度（平台侧异步打包）。"""

    queued = "queued"  # 已提交
    exporting = "exporting"  # 平台打包中
    downloading = "downloading"  # 打包好了，正在下载
    parsing = "parsing"  # 正在解包 / 抽图片 / 匹配学生
    done = "done"
    failed = "failed"


class LabSyncState(str, enum.Enum):
    """实验报告抓附件的进度。

    实验报告是同步下载（比作业快得多），但仍然放到后台线程跑，
    前端轮询这几个状态就行。
    """

    idle = "idle"  # 还没抓过
    running = "running"  # 正在从 i学习 下载 + 解包
    done = "done"
    failed = "failed"


# --------------------------------------------------------------------------- #
# 课程
# --------------------------------------------------------------------------- #
class Course(Base, TimestampMixin):
    """课程。课程是最外层的组织单位，下面挂班级，并有自己的评分细则。"""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, comment="课程名称")
    code: Mapped[str | None] = mapped_column(String(64), comment="课程代码")
    term: Mapped[str | None] = mapped_column(String(64), comment="学期，如 2024-2025-1")
    description: Mapped[str | None] = mapped_column(Text, comment="备注")
    # i学习 侧课程标识，用来把本地课程绑到「我教的课」里的某一门
    istudy_cid: Mapped[str | None] = mapped_column(String(32), comment="i学习 课程 id")
    istudy_cpi: Mapped[str | None] = mapped_column(String(32), comment="i学习 课程 cpi")

    class_links: Mapped[list["CourseClass"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    rubric_links: Mapped[list["CourseRubric"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class CourseClass(Base, TimestampMixin):
    """课程-班级关联。多对多：一个课程有多个班级，一个班级也可以挂多个课程。"""

    __tablename__ = "course_classes"
    __table_args__ = (
        UniqueConstraint("course_id", "class_id", name="uq_course_classes"),
        Index("ix_course_classes_class", "class_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), nullable=False
    )

    course: Mapped["Course"] = relationship(back_populates="class_links")
    class_: Mapped["Class"] = relationship(back_populates="course_links")


class CourseRubric(Base, TimestampMixin):
    """课程-评分标准关联。多对多：一个课程有多份评分细则，一份细则也能被多个课程复用。"""

    __tablename__ = "course_rubrics"
    __table_args__ = (
        UniqueConstraint("course_id", "rubric_id", name="uq_course_rubrics"),
        Index("ix_course_rubrics_rubric", "rubric_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rubric_id: Mapped[int] = mapped_column(
        ForeignKey("rubrics.id", ondelete="CASCADE"), nullable=False
    )

    course: Mapped["Course"] = relationship(back_populates="rubric_links")
    rubric: Mapped["Rubric"] = relationship(back_populates="course_links")


# --------------------------------------------------------------------------- #
# 班级 / 学生
# --------------------------------------------------------------------------- #
class Class(Base, TimestampMixin):
    """班级：由 院系 + 专业 + 班级名称 唯一确定。"""

    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint("department", "major", "name", name="uq_classes_department_major_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    department: Mapped[str] = mapped_column(String(128), default="", nullable=False, comment="院系")
    major: Mapped[str] = mapped_column(String(128), default="", nullable=False, comment="专业")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="班级名称")
    description: Mapped[str | None] = mapped_column(Text, comment="备注")

    students: Mapped[list["Student"]] = relationship(
        back_populates="class_", cascade="all, delete-orphan", order_by="Student.student_no"
    )
    course_links: Mapped[list["CourseClass"]] = relationship(
        back_populates="class_", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        parts = [part for part in (self.department, self.major, self.name) if part]
        return "-".join(parts) if parts else self.name


class Student(Base, TimestampMixin):
    """学生，归属唯一班级。学号 / 工号在班级内唯一。"""

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("class_id", "student_no", name="uq_students_class_student_no"),
        Index("ix_students_student_no", "student_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_no: Mapped[str] = mapped_column(String(64), nullable=False, comment="学号 / 工号")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="姓名")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, comment="加入时间")
    enrollment_year: Mapped[int | None] = mapped_column(Integer, comment="入学年份")
    email: Mapped[str | None] = mapped_column(String(128))
    remark: Mapped[str | None] = mapped_column(Text)

    class_: Mapped["Class"] = relationship(back_populates="students")
    results: Mapped[list["GradingResult"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# 评分细则（= 一个项目 / 一次作业）
# --------------------------------------------------------------------------- #
class Rubric(Base, TimestampMixin):
    """评分细则。一个项目 / 一次作业对应一份，不分版本、不分维度。"""

    __tablename__ = "rubrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="项目 / 作业名称")
    kind: Mapped[GradingKind] = mapped_column(
        _enum_column(GradingKind),
        default=GradingKind.homework,
        nullable=False,
        comment="作业 / 实验报告",
    )
    description: Mapped[str | None] = mapped_column(Text, comment="任务说明 / 题目要求")
    criteria: Mapped[str | None] = mapped_column(Text, comment="评分细则正文，直接作为 AI 评审依据")
    total_score: Mapped[float] = mapped_column(Float, default=100.0, nullable=False, comment="满分")
    extra_prompt: Mapped[str | None] = mapped_column(Text, comment="附加给 AI 的提示词")
    istudy_task_id: Mapped[str | None] = mapped_column(
        String(32), index=True, comment="i学习 批次 id（第一次见到的那个），仅作追溯"
    )
    istudy_library_id: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        comment="i学习 作业题库 id：一次作业发给多个班时这个是一样的，是细则的真正归属键",
    )
    source: Mapped[str] = mapped_column(
        String(16), default="manual", nullable=False, comment="manual=手工建 / istudy=从 i学习 同步"
    )

    results: Mapped[list["GradingResult"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan"
    )
    course_links: Mapped[list["CourseRubric"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan"
    )
    works: Mapped[list["IstudyWork"]] = relationship(back_populates="rubric")


# --------------------------------------------------------------------------- #
# 评分结果
# --------------------------------------------------------------------------- #
class GradingResult(Base, TimestampMixin):
    """评分结果：一个评分细则 × 一个学生 = 一条记录（含总分与评语）。

    class_id 冗余存一份，方便直接按「项目 × 班级」出成绩。
    """

    __tablename__ = "grading_results"
    __table_args__ = (
        UniqueConstraint("rubric_id", "student_id", name="uq_results_rubric_student"),
        Index("ix_results_rubric_class", "rubric_id", "class_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rubric_id: Mapped[int] = mapped_column(
        ForeignKey("rubrics.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_filename: Mapped[str] = mapped_column(String(300), nullable=False, comment="用于评审的文件名")
    object_key: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="文件在对象存储里的相对 key"
    )
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(
        String(64), comment="内容指纹（抽出来的图片 + 文字），换文件才重评"
    )
    match_reason: Mapped[str | None] = mapped_column(String(200), comment="如何匹配到这个学生")

    status: Mapped[ResultStatus] = mapped_column(
        _enum_column(ResultStatus), default=ResultStatus.pending, nullable=False
    )
    score: Mapped[float | None] = mapped_column(Float, comment="总分")
    comment: Mapped[str | None] = mapped_column(Text, comment="评语")
    model: Mapped[str | None] = mapped_column(String(120), comment="使用的模型")
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    graded_at: Mapped[datetime | None] = mapped_column(DateTime, comment="评审时间")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, comment="送进模型的 token 数")
    completion_tokens: Mapped[int | None] = mapped_column(Integer, comment="模型输出的 token 数")
    images_sent: Mapped[int | None] = mapped_column(Integer, comment="送给模型的图片张数")
    raw_response: Mapped[str | None] = mapped_column(Text, comment="模型原始返回，便于排查")
    error_message: Mapped[str | None] = mapped_column(Text)
    is_manual: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否人工调整过分数或评语"
    )

    rubric: Mapped["Rubric"] = relationship(back_populates="results")
    student: Mapped["Student"] = relationship(back_populates="results")
    class_: Mapped["Class"] = relationship()


# --------------------------------------------------------------------------- #
# i学习 作业
# --------------------------------------------------------------------------- #
class IstudyWork(Base, TimestampMixin):
    """i学习 的一次作业 + 一个班级 = 一行。

    i学习 的模型是两层：
      一次作业发布 = ``istudy_task_id``（如「第1周作业」= 140841），它可以发给多个班；
      每个班在这个作业下有独立的 ``istudy_work_id``（如 25人工智能本1 = 265355），
      各自有独立的作答时间、状态和「已交 / 未交 / 待批」统计。

    ``submitted_count`` / ``unsubmitted_count`` / ``pending_count`` 是 i学习 列表页上的
    统计口径：同一个 task 的多个班级行会拿到同一组数字（列表页按发布批次统计），
    真正按班级的精确人数以本地的 ``istudy_submissions`` 为准。
    """

    __tablename__ = "istudy_works"
    __table_args__ = (
        UniqueConstraint("istudy_work_id", name="uq_istudy_works_work_id"),
        Index("ix_istudy_works_course_class_status", "course_id", "class_id", "status"),
        Index("ix_istudy_works_task", "istudy_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), index=True, comment="本地班级，名单没导入时为空"
    )
    rubric_id: Mapped[int | None] = mapped_column(
        ForeignKey("rubrics.id", ondelete="SET NULL"), index=True, comment="绑定的评分细则"
    )

    istudy_cid: Mapped[str] = mapped_column(String(32), nullable=False, comment="i学习 课程 id")
    istudy_clazzid: Mapped[str] = mapped_column(String(32), nullable=False, comment="i学习 班级 id")
    istudy_task_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="i学习 批次 id")
    istudy_library_id: Mapped[str | None] = mapped_column(
        String(64), index=True, comment="i学习 作业题库 id，用来认出「同一次作业的不同批次」"
    )
    istudy_work_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="i学习 作业-班级 id")

    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="作业名，如 第1周作业")
    status: Mapped[WorkStatus] = mapped_column(
        _enum_column(WorkStatus), default=WorkStatus.ongoing, nullable=False
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime, comment="作答开始时间")
    end_at: Mapped[datetime | None] = mapped_column(DateTime, comment="作答截止时间")
    time_text: Mapped[str | None] = mapped_column(String(120), comment="列表页原始时间文案")

    submitted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="已交")
    unsubmitted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="未交")
    pending_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="待批")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)

    course: Mapped["Course"] = relationship()
    class_: Mapped["Class"] = relationship()
    rubric: Mapped["Rubric"] = relationship(back_populates="works")
    submissions: Mapped[list["IstudySubmission"]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )
    exports: Mapped[list["IstudyExport"]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )


class IstudySubmission(Base, TimestampMixin):
    """某学生在某次作业下的提交情况（一个 work × 一个学生 = 一行）。

    没交的学生也会有一行（``state=missing``、``file_state=none``），这样「谁没交」
    不用去猜。真正的评审结果仍然落在 ``grading_results``。
    """

    __tablename__ = "istudy_submissions"
    __table_args__ = (
        UniqueConstraint("work_id", "student_id", name="uq_istudy_submissions_work_student"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("istudy_works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )

    istudy_answer_id: Mapped[str | None] = mapped_column(String(32), comment="i学习 答题记录 id")
    istudy_user_id: Mapped[str | None] = mapped_column(String(32), comment="i学习 用户 id")
    state: Mapped[SubmissionState] = mapped_column(
        _enum_column(SubmissionState), default=SubmissionState.missing, nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="i学习 上的提交时间")
    istudy_score: Mapped[float | None] = mapped_column(Float, comment="i学习 上已有的分数，用于对照")

    source_filename: Mapped[str | None] = mapped_column(String(300), comment="学生提交包 / 原文件名")
    object_key: Mapped[str | None] = mapped_column(String(500), comment="整包文件在对象存储的 key")
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), comment="原始文件 sha256")
    content_checksum: Mapped[str | None] = mapped_column(
        String(64),
        comment="内容指纹（抽出来的图片 + 文字）：i学习 每次导出的外层文件字节会变，"
        "但内容不变，用它判断学生有没有真的重新交",
    )

    file_state: Mapped[FileState] = mapped_column(
        _enum_column(FileState), default=FileState.none, nullable=False
    )
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="抽出的作答图片数")
    extracted_text: Mapped[str | None] = mapped_column(Text, comment="PDF 里抽出的文字（题干 / 题号等）")
    error_message: Mapped[str | None] = mapped_column(Text)

    work: Mapped["IstudyWork"] = relationship(back_populates="submissions")
    student: Mapped["Student"] = relationship()
    files: Mapped[list["IstudySubmissionFile"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", order_by="IstudySubmissionFile.seq"
    )


class IstudySubmissionFile(Base, TimestampMixin):
    """学生提交里拆出来的单个文件。

    作业基本都是学生拍照上传，所以这里主要就是「作答图片」：从 i学习 导出的 PDF 里
    按页顺序抠出来，后面直接按 seq 顺序丢给多模态模型评审。
    """

    __tablename__ = "istudy_submission_files"
    __table_args__ = (Index("ix_istudy_submission_files_submission", "submission_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("istudy_submissions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="顺序，从 1 开始")
    role: Mapped[str] = mapped_column(
        String(16), default="answer", nullable=False, comment="answer=作答 / question=题面 / other"
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))

    submission: Mapped["IstudySubmission"] = relationship(back_populates="files")


class IstudyExport(Base, TimestampMixin):
    """i学习「导出作业附件」的任务记录。

    导出不是即时的：packWork 只是入队，实测一个班 33 人要 3 分钟左右。
    所以要把任务落库，前端轮询进度。
    """

    __tablename__ = "istudy_exports"
    __table_args__ = (Index("ix_istudy_exports_work", "work_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("istudy_works.id", ondelete="CASCADE"), nullable=False, index=True
    )

    content: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="0 完整答题记录 / 1 仅提交附件")
    fmt: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="0 Word / 1 PDF")
    pack_scope: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="0 本班级 / 1 全部班级")
    person_ids: Mapped[str | None] = mapped_column(
        Text, comment="只导出指定学生时，这里存 i学习 的用户 id（逗号分隔）"
    )

    istudy_download_id: Mapped[str | None] = mapped_column(String(32), comment="下载中心的条目 id")
    status: Mapped[ExportStatus] = mapped_column(
        _enum_column(ExportStatus), default=ExportStatus.queued, nullable=False
    )
    download_url: Mapped[str | None] = mapped_column(Text, comment="带签名的临时直链，会过期")
    object_key: Mapped[str | None] = mapped_column(String(500), comment="整包 zip 落库后的 key")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="包里识别出的学生数")
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    work: Mapped["IstudyWork"] = relationship(back_populates="exports")


# --------------------------------------------------------------------------- #
# i学习 实验报告
# --------------------------------------------------------------------------- #
class IstudyLabReport(Base, TimestampMixin):
    """i学习 的一次实验报告 + 一个班级 = 一行。

    i学习 实验室（mooc.istudy.szpu.edu.cn/lab）里，一个报告记录（reportId）可以
    同时发给好几个班（``noticeMoocClassIds``），学生名单在 ``stuList`` 里按学号给。
    本地按「班级」拆成一行，这样一个班就对应磁盘上一个文件夹，
    跟作业那边的 ``istudy_works`` 是一个思路。

    ``type``：1 表单型 / 2 附件型 / 3 Word 模板型。附件型才是学生上传 PDF 的那种，
    但三种都能走「导出作答」，所以都存下来，前端照实展示。
    """

    __tablename__ = "istudy_lab_reports"
    __table_args__ = (
        UniqueConstraint(
            "istudy_report_id", "istudy_class_id", name="uq_istudy_lab_reports_report_class"
        ),
        Index("ix_istudy_lab_reports_course_class", "course_id", "class_id"),
        Index("ix_istudy_lab_reports_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[int | None] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), index=True, comment="本地班级"
    )
    rubric_id: Mapped[int | None] = mapped_column(
        ForeignKey("rubrics.id", ondelete="SET NULL"), index=True, comment="绑定的评分细则"
    )

    istudy_cid: Mapped[str] = mapped_column(String(32), nullable=False, comment="i学习 课程 id")
    istudy_report_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="i学习 实验报告 id（reportId）"
    )
    istudy_class_id: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="i学习 班级 id（noticeMoocClassIds 里的一项）"
    )
    istudy_dept_id: Mapped[str | None] = mapped_column(
        String(32), comment="导出接口要的 deptId / fid"
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="实验报告名")
    report_type: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False, comment="1 表单 / 2 附件 / 3 Word 模板"
    )
    istudy_status: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="i学习 上报告的启停状态"
    )
    tip_text: Mapped[str | None] = mapped_column(
        String(32),
        comment="i学习 列表页的状态角标：未开始 / 已开始 / 已截止。"
        "「未开始」= 老师预填好但还没发布，只能以这个为准，别拿时间猜",
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime, comment="作答开始时间")
    end_at: Mapped[datetime | None] = mapped_column(DateTime, comment="作答截止时间")

    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="应作答人数")
    submitted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="已交")
    unsubmitted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="未交")

    sync_state: Mapped[LabSyncState] = mapped_column(
        _enum_column(LabSyncState), default=LabSyncState.idle, nullable=False
    )
    sync_message: Mapped[str | None] = mapped_column(Text)
    sync_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    sync_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, comment="上次同步名单时间")

    course: Mapped["Course"] = relationship()
    class_: Mapped["Class"] = relationship()
    rubric: Mapped["Rubric"] = relationship()
    submissions: Mapped[list["IstudyLabSubmission"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class IstudyLabSubmission(Base, TimestampMixin):
    """某学生在某次实验报告下的提交情况（一个报告行 × 一个学生 = 一行）。

    跟作业一样，没交的学生也会有一行（``state=missing``），这样「谁没交」不用猜。
    ``istudy_fill_id`` 是 i学习 的作答记录 id，导出作答时要传它。
    """

    __tablename__ = "istudy_lab_submissions"
    __table_args__ = (
        UniqueConstraint("report_id", "student_id", name="uq_istudy_lab_submissions_report_student"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("istudy_lab_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )

    istudy_fill_id: Mapped[str | None] = mapped_column(String(32), comment="i学习 作答记录 id")
    istudy_user_id: Mapped[str | None] = mapped_column(String(32), comment="i学习 用户 id")
    state: Mapped[SubmissionState] = mapped_column(
        _enum_column(SubmissionState), default=SubmissionState.missing, nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, comment="i学习 上的提交时间")
    istudy_score: Mapped[float | None] = mapped_column(Float, comment="i学习 上已有的分数，用于对照")

    source_filename: Mapped[str | None] = mapped_column(String(300), comment="学生提交的 PDF 文件名")
    object_key: Mapped[str | None] = mapped_column(String(500), comment="PDF 在对象存储的 key")
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), comment="原始文件 sha256")
    content_checksum: Mapped[str | None] = mapped_column(
        String(64), comment="内容指纹（抽出来的图片 + 文字），换文件才重评"
    )

    file_state: Mapped[FileState] = mapped_column(
        _enum_column(FileState), default=FileState.none, nullable=False
    )
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="抽出的作答图片数")
    extracted_text: Mapped[str | None] = mapped_column(Text, comment="PDF 里抽出的文字")
    error_message: Mapped[str | None] = mapped_column(Text)

    report: Mapped["IstudyLabReport"] = relationship(back_populates="submissions")
    student: Mapped["Student"] = relationship()
    files: Mapped[list["IstudyLabFile"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", order_by="IstudyLabFile.seq"
    )


class IstudyLabFile(Base, TimestampMixin):
    """实验报告提交里拆出来的作答图片（从学生交的 PDF 里按页抠出来）。"""

    __tablename__ = "istudy_lab_files"
    __table_args__ = (Index("ix_istudy_lab_files_submission", "submission_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("istudy_lab_submissions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="顺序，从 1 开始")
    role: Mapped[str] = mapped_column(
        String(16), default="answer", nullable=False, comment="answer=作答 / question=题面 / other"
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))

    submission: Mapped["IstudyLabSubmission"] = relationship(back_populates="files")
