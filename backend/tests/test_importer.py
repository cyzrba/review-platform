"""名单导入解析的单元测试。"""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from app.services.importer import (
    ImportError_,
    parse_enrollment_year,
    parse_joined_at,
    parse_roster,
)


def test_parse_csv_with_all_columns():
    data = (
        "学号,姓名,院系,专业,班级,加入时间,入学年份\n"
        "2023010101,张三,计算机学院,软件工程,软工2301,2023-09-01,2023\n"
        "2023010102,李四,计算机学院,软件工程,软工2301,2023/09/02,2023级\n"
    ).encode("utf-8-sig")
    parsed = parse_roster("roster.csv", data)

    assert len(parsed.rows) == 2
    first = parsed.rows[0]
    assert first["student_no"] == "2023010101"
    assert first["name"] == "张三"
    assert first["department"] == "计算机学院"
    assert first["major"] == "软件工程"
    assert first["class_name"] == "软工2301"
    assert first["joined_at"] == datetime(2023, 9, 1)
    assert first["enrollment_year"] == 2023
    assert parsed.rows[1]["joined_at"] == datetime(2023, 9, 2)
    assert parsed.rows[1]["enrollment_year"] == 2023


def test_parse_xlsx_with_date_cells_and_numeric_student_no():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["工号", "姓名", "学院", "专业", "班级", "加入时间", "年级"])
    sheet.append([2023010101, "张三", "计算机学院", "软件工程", "软工2301", datetime(2023, 9, 1), "2023"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_roster("roster.xlsx", buffer.getvalue())
    row = parsed.rows[0]
    assert row["student_no"] == "2023010101"  # 数字单元格不能变成 2023010101.0
    assert row["department"] == "计算机学院"
    assert row["joined_at"] == datetime(2023, 9, 1)
    assert row["enrollment_year"] == 2023


def test_missing_required_columns_raises():
    data = "学号,院系\n2023010101,计算机学院\n".encode("utf-8")
    with pytest.raises(ImportError_):
        parse_roster("roster.csv", data)


def test_duplicate_and_blank_rows_are_skipped():
    data = (
        "学号,姓名,班级\n"
        "2023010101,张三,软工2301\n"
        "2023010101,张三重复,软工2301\n"
        ",没有学号,软工2301\n"
    ).encode("utf-8")
    parsed = parse_roster("roster.csv", data)
    assert len(parsed.rows) == 1
    assert len(parsed.warnings) == 2


def test_gbk_encoded_csv():
    data = "学号,姓名,班级\n2023010101,张三,软工2301\n".encode("gbk")
    parsed = parse_roster("roster.csv", data)
    assert parsed.rows[0]["name"] == "张三"


def test_parse_joined_at_and_year_helpers():
    assert parse_joined_at("2023年9月1日") == datetime(2023, 9, 1)
    assert parse_joined_at("") is None
    assert parse_enrollment_year("2023级") == 2023
    assert parse_enrollment_year(2024) == 2024
    assert parse_enrollment_year("不是年份") is None
