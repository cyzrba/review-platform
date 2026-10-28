"""评分细则 Word / PDF 材料的保存、解析和评审上下文。"""

from __future__ import annotations

import io
import zipfile
from uuid import uuid4

import pymupdf

from app.services.llm_client import ImagePart
from app.services.reviewer import ReviewContext, build_review_prompt


def _docx_bytes(text: str) -> bytes:
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", body)
    return buffer.getvalue()


def _pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    payload = document.tobytes()
    document.close()
    return payload


def test_upload_and_replace_homework_materials(client):
    tag = uuid4().hex[:8]
    rubric_name = f"材料测试-{tag}"
    rubric = client.post(
        "/api/rubrics",
        json={
            "name": rubric_name,
            "kind": "homework",
            "total_score": 100,
        },
    ).json()

    response = client.post(
        f"/api/rubrics/{rubric['id']}/materials/criteria",
        files={
            "file": (
                "评分标准.docx",
                _docx_bytes("第一题 60 分；第二题 40 分"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200, response.text
    criteria = next(item for item in response.json()["attachments"] if item["role"] == "criteria")
    assert criteria["filename"] == "评分标准.docx"
    assert criteria["object_key"] == f"rubrics/{rubric_name}/word/评分标准.docx"
    assert criteria["page_count"] == 0
    preview = client.get(f"/api/rubrics/{rubric['id']}/materials/criteria/preview").json()
    assert "第一题 60 分" in preview["extracted_text"]

    response = client.post(
        f"/api/rubrics/{rubric['id']}/materials/reference",
        files={"file": ("原题与答案.pdf", _pdf_bytes("Question and standard answer"), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    reference = next(item for item in response.json()["attachments"] if item["role"] == "reference")
    assert reference["page_count"] == 1
    assert reference["object_key"] == f"rubrics/{rubric_name}/pdf/原题与答案.pdf"
    assert reference["size_bytes"] > 0
    preview = client.get(f"/api/rubrics/{rubric['id']}/materials/reference/preview").json()
    assert len(preview["page_keys"]) == 1

    invalid = client.post(
        f"/api/rubrics/{rubric['id']}/materials/criteria",
        files={"file": ("错误.pdf", b"not a pdf", "application/pdf")},
    )
    assert invalid.status_code == 400


def test_review_prompt_contains_word_criteria_and_reference_pdf():
    context = ReviewContext(
        rubric_name="第七周作业",
        kind="homework",
        rubric_description=None,
        criteria=None,
        extra_prompt=None,
        total_score=100,
        student_no="20260001",
        student_name="张三",
        filename="answer.pdf",
        content_type="application/pdf",
        file_bytes=b"",
        criteria_document_text="第一题 75 分；第二题 25 分",
        reference_text="单缝衍射标准答案",
        images=[
            ImagePart(filename="reference-p1.png", content_type="image/png", data=b"x", role="reference"),
            ImagePart(filename="answer-p1.png", content_type="image/png", data=b"x", role="answer"),
        ],
    )
    prompt = build_review_prompt(context)
    assert "【Word 评分标准】" in prompt
    assert "第一题 75 分；第二题 25 分" in prompt
    assert "【PDF 原题与标准答案文字】" in prompt
    assert "最前面 1 张" in prompt
    assert "最后 1 张" in prompt
