"""进程内冒烟：跑一遍完整流程，按当前 .env / 环境变量选择存储后端。

用法：
    # 本地磁盘（不需要 MinIO）
    ./.venv/bin/python scripts/smoke_test.py

    # 本地 MinIO
    STORAGE_BACKEND=minio MINIO_ENDPOINT=localhost:9000 \
    MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin123 \
    ./.venv/bin/python scripts/smoke_test.py
"""

from __future__ import annotations

import io
import sys
import time
import zipfile

from fastapi.testclient import TestClient

from app.main import app


def build_zip(students: list[tuple[str, str]]) -> bytes:
    """实验报告：外层压缩包里是 学生名字_学号.zip，里面是 学生名字_学号.pdf。"""
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
        for no, name in students:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as student_zip:
                student_zip.writestr(f"{name}_{no}.pdf", f"{name} 的实验报告正文……".encode("utf-8"))
            archive.writestr(f"{name}_{no}.zip", inner.getvalue())
        archive.writestr("无名氏.zip", b"not-a-real-zip")
    return outer.getvalue()


def main() -> int:
    tag = str(int(time.time()))
    department = f"计算机学院{tag}"
    # 学号每次运行都换，避免多次冒烟后系统里出现重复学号（现实中它是全校唯一的）
    base = f"2023{tag[-6:]}"
    students = [(f"{base}01", "张三"), (f"{base}02", "李四")]
    course_name = f"数据结构-{tag}"
    roster = (
        "学号,姓名,院系,专业,班级,加入时间,入学年份\n"
        f"{students[0][0]},张三,{department},软件工程,软工2301,2023-09-01,2023\n"
        f"{students[1][0]},李四,{department},软件工程,软工2301,2023-09-01,2023\n"
    ).encode("utf-8-sig")

    with TestClient(app) as client:
        health = client.get("/api/health").json()
        print("健康检查:", health)
        if not health["storage"]["ok"]:
            print("!! 对象存储不可用，冒烟失败")
            return 1

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

        rubric = client.post(
            "/api/rubrics",
            json={
                "name": f"实验一 数据采集 {tag}",
                "kind": "lab_report",
                "description": "完成数据采集并撰写实验报告",
                "criteria": "按完成度给分：内容完整 60 分，结论合理 40 分；抄袭或未交记 0 分。",
                "extra_prompt": "重点关注实验步骤的可复现性",
                "total_score": 100,
                "course_ids": [course["id"]],
            },
        ).json()
        rubric_id = rubric["id"]
        print(f"创建评分细则 #{rubric_id}：{rubric['name']}")

        upload = client.post(
            f"/api/rubrics/{rubric_id}/submissions",
            files={
                "files": (
                    "实验一批次.zip",
                    build_zip(students),
                    "application/zip",
                )
            },
        ).json()
        print("上传压缩包:", upload["message"])

        run = client.post(
            f"/api/rubrics/{rubric_id}/grade", json={}, params={"sync": "true"}
        ).json()
        print("AI 评审:", run["message"], run["result_ids"])

        for row in client.get(f"/api/rubrics/{rubric_id}/results").json()["items"]:
            print(f"  {row['student_no']} {row['student_name']}: {row['score']} 分 — {row['comment']}")

        export = client.get(f"/api/rubrics/{rubric_id}/export.xlsx")
        print(f"导出 Excel: {len(export.content)} bytes, HTTP {export.status_code}")

        first = client.get(f"/api/rubrics/{rubric_id}/results").json()["items"][0]
        fetched = client.get("/api/files/download", params={"key": first["object_key"]})
        print(f"从对象存储取回提交文件: {len(fetched.content)} bytes, HTTP {fetched.status_code}")

        print("\n冒烟通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
