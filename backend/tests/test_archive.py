"""压缩包展开、文件筛选与学生匹配的单元测试。"""

from __future__ import annotations

import io
import zipfile

from app.services.archive import (
    StudentRef,
    assign_students,
    decode_zip_name,
    match_student,
    scan_uploads,
    score_candidate,
    select_documents,
)

STUDENTS = [
    StudentRef(1, "2023010101", "张三", "软工2301"),
    StudentRef(2, "2023010102", "李四", "软工2301"),
    StudentRef(3, "2023010103", "王五", "计科2302"),
    StudentRef(4, "2021001", "赵六", "软工2301"),
    StudentRef(5, "20210012", "钱七", "软工2301"),
]


def zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 中文文件名
# --------------------------------------------------------------------------- #
def test_decode_gbk_filename():
    raw = "张三的实验报告.pdf".encode("gbk").decode("cp437")
    assert decode_zip_name(raw, 0) == "张三的实验报告.pdf"


def test_decode_utf8_flag_keeps_name():
    assert decode_zip_name("张三.pdf", 0x800) == "张三.pdf"


# --------------------------------------------------------------------------- #
# 展开与筛选
# --------------------------------------------------------------------------- #
def test_lab_report_unwraps_student_zip():
    """实验报告：外层压缩包里是 学生名字_学号.zip，里面是 学生名字_学号.pdf。"""
    inner = zip_bytes({"张三_2023010101.pdf": b"%PDF-1.4 fake"})
    outer = zip_bytes(
        {
            "张三_2023010101.zip": inner,
            "李四_2023010102.zip": zip_bytes({"李四_2023010102.pdf": b"%PDF-1.4 fake"}),
            ".DS_Store": b"junk",
            "__MACOSX/._x": b"junk",
        }
    )
    scan = scan_uploads([("实验一批次.zip", outer)])
    documents, skipped = select_documents(scan.leaves, "lab_report")

    assert [doc.name for doc in documents] == ["张三_2023010101.pdf", "李四_2023010102.pdf"]
    assert documents[0].parents == ["张三_2023010101.zip"]
    assert skipped == []


def test_homework_keeps_only_word_files():
    """作业：压缩包里既有压缩包也有 word，只关心 word。"""
    nested = zip_bytes({"计算机学院-软件工程-软工2301-2023010102-李四.docx": b"docx"})
    outer = zip_bytes(
        {
            "计算机学院-软件工程-软工2301-2023010101-张三.docx": b"docx",
            "素材.zip": nested,
            "说明.txt": "忽略我",
            "图片.png": b"\x89PNG",
        }
    )
    scan = scan_uploads([("作业一.zip", outer)])
    documents, skipped = select_documents(scan.leaves, "homework")

    assert sorted(doc.name for doc in documents) == [
        "计算机学院-软件工程-软工2301-2023010101-张三.docx",
        "计算机学院-软件工程-软工2301-2023010102-李四.docx",
    ]
    # docx 优先排前
    assert documents[0].name.endswith("张三.docx")
    assert {issue.filename for issue in skipped} == {"说明.txt", "图片.png"}


def test_bad_zip_is_reported_not_raised():
    scan = scan_uploads([("坏包.zip", b"not a zip at all")])
    assert scan.leaves == []
    assert "不是有效的 zip" in scan.issues[0].reason


# --------------------------------------------------------------------------- #
# 学生匹配
# --------------------------------------------------------------------------- #
def test_match_homework_filename_by_no_and_name():
    result = match_student(
        ["计算机学院-软件工程-软工2301-2023010101-张三.docx"], STUDENTS
    )
    assert result.student_id == 1
    assert result.score == 100
    assert "学号" in result.reason and "姓名" in result.reason


def test_match_lab_report_inner_file():
    result = match_student(["李四_2023010102.pdf", "李四_2023010102.zip"], STUDENTS)
    assert result.student_id == 2


def test_match_by_name_segment():
    """姓名必须是一个独立片段（用分隔符隔开）才直接采用。"""
    result = match_student(["王五-实验报告.docx"], STUDENTS)
    assert result.student_id == 3
    assert result.score == 88


def test_name_embedded_in_chinese_text_is_not_used():
    """「王五的实验报告」不直接采用，但要给出可操作的提示。"""
    result = match_student(["王五的实验报告.docx"], STUDENTS)
    assert result.student_id is None
    assert "王五" in result.reason
    assert "建议命名成" in result.reason


def test_no_false_positive_on_similar_student_numbers():
    """2021001 不能命中 20210012 的文件。"""
    assert score_candidate("20210012-钱七.docx", "2021001", "赵六")[0] < 88
    result = match_student(["20210012-钱七.docx"], STUDENTS)
    assert result.student_id == 5


def test_no_false_positive_on_name_prefix():
    """李四 不能命中 李四光 的文件（都不是本校学生时应判未匹配）。"""
    students = [StudentRef(1, "2023010101", "李四", "软工2301")]
    result = match_student(["2023010102-李四光.docx"], students)
    assert result.student_id is None


def test_unmatched_without_identity():
    result = match_student(["实验报告最终版.docx"], STUDENTS)
    assert result.student_id is None
    assert "找不到" in result.reason


def test_ambiguous_when_two_students_tie():
    """同名同姓，且文件名里没有班级名可以区分时，判为歧义。"""
    students = [
        StudentRef(1, "2023010101", "张三", "软工2301"),
        StudentRef(2, "2023010102", "张三", "计科2302"),
    ]
    result = match_student(["张三-实验报告.docx"], students)
    assert result.student_id is None
    assert result.ambiguous is True


def test_tie_broken_by_class_name_in_filename():
    """同学号跨班级（比如重修）时，用文件名里的班级名区分。"""
    students = [
        StudentRef(1, "2023010101", "张三", "软工2301"),
        StudentRef(2, "2023010101", "张三", "计科2302"),
    ]
    result = match_student(["计算机学院-计算机科学与技术-计科2302-2023010101-张三.docx"], students)
    assert result.student_id == 2
    assert "班级名" in result.reason


def test_assign_students_keeps_one_file_per_student():
    scan = scan_uploads(
        [
            (
                "作业一.zip",
                zip_bytes(
                    {
                        "计算机学院-软件工程-软工2301-2023010101-张三.docx": b"docx",
                        "计算机学院-软件工程-软工2301-2023010101-张三-副本.docx": b"docx",
                        "无主文件.docx": b"docx",
                    }
                ),
            )
        ]
    )
    documents, _ = select_documents(scan.leaves, "homework")
    matched, issues = assign_students(documents, STUDENTS)

    assert len(matched) == 1
    assert matched[0].student_id == 1
    reasons = " ".join(issue.reason for issue in issues)
    assert "已用" in reasons
    assert "找不到学号或姓名" in reasons
