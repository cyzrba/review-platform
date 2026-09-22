"""对着真实运行中的后端跑一遍端到端流程（HTTP，不是 TestClient）。

用法：
    ./.venv/bin/python scripts/e2e_http.py http://127.0.0.1:8010
"""

from __future__ import annotations

import io
import sys
import time
import zipfile

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010").rstrip("/")


def lab_report_zip(students: list[tuple[str, str]]) -> bytes:
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        for no, name in students:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as student_zip:
                student_zip.writestr(f"{name}_{no}.pdf", f"{name} 的实验报告正文".encode("utf-8"))
            archive.writestr(f"{name}_{no}.zip", inner.getvalue())
    return outer.getvalue()


def homework_zip(department: str, rows: list[tuple[str, str, str, str]]) -> bytes:
    """作业：压缩包里既有 word 也有别的压缩包，只认 word。"""
    outer = io.BytesIO()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as inner:
        no, name, major, klass = rows[1]
        inner.writestr(f"{department}-{major}-{klass}-{no}-{name}.docx", b"docx")
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        no, name, major, klass = rows[0]
        archive.writestr(f"{department}-{major}-{klass}-{no}-{name}.docx", b"docx")
        archive.writestr("汇总.zip", nested.getvalue())
        archive.writestr("无关素材.txt", "忽略")
    return outer.getvalue()


def main() -> int:
    tag = str(int(time.time()))
    department = f"计算机学院{tag}"
    # 学号每次运行都换，避免多次联调后系统里出现重复学号（现实中它是全校唯一的）
    base = f"2023{tag[-6:]}"
    no_zhang, no_li, no_wang = f"{base}01", f"{base}02", f"{base}03"
    course_name = f"数据结构-{tag}"
    roster = (
        "学号,姓名,院系,专业,班级,加入时间,入学年份\n"
        f"{no_zhang},张三,{department},软件工程,软工2301,2023-09-01,2023\n"
        f"{no_li},李四,{department},软件工程,软工2301,2023-09-01,2023\n"
        f"{no_wang},王五,{department},计算机科学与技术,计科2302,2023-09-01,2023\n"
    ).encode("utf-8-sig")

    # trust_env=False：避免读到系统代理设置，本地联调直连
    with httpx.Client(base_url=BASE, timeout=120, trust_env=False) as client:
        print("健康检查:", client.get("/api/health").json())

        course = client.post(
            "/api/courses",
            json={"name": course_name, "code": f"CS{tag[-4:]}", "term": "2024-2025-1"},
        ).json()
        print(f"创建课程 #{course['id']}：{course['name']}")

        imported = client.post(
            "/api/classes/import",
            files={"file": ("roster.csv", roster, "text/csv")},
            data={"course_id": str(course["id"])},
        ).json()
        print("导入名单:", imported["message"])

        for kind, name, payload in (
            (
                "lab_report",
                f"实验一 数据采集 {tag}",
                lab_report_zip([(no_zhang, "张三"), (no_li, "李四")]),
            ),
            (
                "homework",
                f"第 3 次作业 {tag}",
                homework_zip(
                    department,
                    [
                        (no_zhang, "张三", "软件工程", "软工2301"),
                        (no_wang, "王五", "计算机科学与技术", "计科2302"),
                    ],
                ),
            ),
        ):
            rubric = client.post(
                "/api/rubrics",
                json={
                    "name": name,
                    "kind": kind,
                    "description": "按任务要求完成并提交",
                    "criteria": "内容完整 60 分，结论合理 40 分。",
                    "total_score": 100,
                    "course_ids": [course["id"]],
                },
            ).json()
            rubric_id = rubric["id"]
            print(f"\n[{kind}] 评分细则 #{rubric_id}：{rubric['name']}")

            upload = client.post(
                f"/api/rubrics/{rubric_id}/submissions",
                files={"files": (f"{name}.zip", payload, "application/zip")},
            ).json()
            print("  上传:", upload["message"])
            if upload["issues"]:
                for issue in upload["issues"][:2]:
                    print(f"    跳过 {issue['filename']}：{issue['reason']}")

            run = client.post(
                f"/api/rubrics/{rubric_id}/grade", json={}, params={"sync": "true"}
            ).json()
            print("  评审:", run["message"], run["result_ids"])

            for row in client.get(f"/api/rubrics/{rubric_id}/results").json()["items"]:
                print(f"  {row['student_no']} {row['student_name']}: {row['score']} 分")

            export = client.get(f"/api/rubrics/{rubric_id}/export.xlsx")
            print(f"  导出 Excel: {len(export.content)} bytes, HTTP {export.status_code}")

        print("\n真实 HTTP 端到端通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
