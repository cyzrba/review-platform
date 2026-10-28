"""实验报告 PDF 按页渲染，保证视觉模型拿到完整页面。"""

from __future__ import annotations

import pymupdf
import pytest

from app.services.answer_files import (
    AnswerFileError,
    extract_lab_report,
    prepare_for_llm,
)

def test_lab_report_renders_every_page_and_keeps_text():
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Experiment purpose", fontsize=12)
    document.new_page().insert_text((72, 72), "Data processing", fontsize=12)
    payload = document.tobytes()
    document.close()

    bundle = extract_lab_report("student.pdf", payload)

    assert len(bundle.images) == 2
    assert [item.filename for item in bundle.images] == [
        "student-p1.png",
        "student-p2.png",
    ]
    assert all(item.data.startswith(b"\x89PNG\r\n\x1a\n") for item in bundle.images)
    assert "Experiment purpose" in bundle.text
    assert "Data processing" in bundle.text


def test_lab_report_rejects_non_pdf():
    with pytest.raises(AnswerFileError, match="暂不支持"):
        extract_lab_report("student.docx", b"not a pdf")


def test_rendered_page_is_compressed_for_llm():
    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(20, 20, 500, 800), color=(0, 0, 0), fill=(1, 1, 1))
    payload = document.tobytes()
    document.close()

    bundle = extract_lab_report("student.pdf", payload)
    prepared, content_type = prepare_for_llm(bundle.images[0].data, max_edge=800, quality=80)

    pixmap = pymupdf.Pixmap(prepared)
    assert content_type == "image/jpeg"
    assert max(pixmap.width, pixmap.height) <= 800
