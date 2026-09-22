"""数据模型（SQLite 表结构）。

一共 7 张表：
    courses          课程
    course_classes   课程-班级关联（一个课程多个班级，一个班级可挂多个课程）
    course_rubrics   课程-评分标准关联（一个课程多份评分细则）
    classes          班级（院系 + 专业 + 班级名称）
    students         学生（学号/工号、姓名、加入时间、入学年份）
    rubrics          评分细则（一个项目/一次作业对应一份，不分版本、不分维度）
    grading_results  评分结果（某评分细则下、某班级、某学生的总分与评语）
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

    results: Mapped[list["GradingResult"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan"
    )
    course_links: Mapped[list["CourseRubric"]] = relationship(
        back_populates="rubric", cascade="all, delete-orphan"
    )


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
    object_key: Mapped[str] = mapped_column(String(500), nullable=False, comment="文件在 MinIO 的 key")
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), comment="文件 sha256")
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
    raw_response: Mapped[str | None] = mapped_column(Text, comment="模型原始返回，便于排查")
    error_message: Mapped[str | None] = mapped_column(Text)
    is_manual: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否人工调整过分数或评语"
    )

    rubric: Mapped["Rubric"] = relationship(back_populates="results")
    student: Mapped["Student"] = relationship(back_populates="results")
    class_: Mapped["Class"] = relationship()
