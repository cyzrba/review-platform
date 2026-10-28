"""评分细则材料：评分标准 Word、原题与标准答案 PDF。"""

from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Rubric, RubricAttachment
from ..storage import get_storage
from ..utils import safe_filename, sha256_hex
from .answer_files import AnswerFileError, render_pdf_pages

ROLE_CRITERIA = "criteria"
ROLE_REFERENCE = "reference"
ATTACHMENT_ROLES = {ROLE_CRITERIA, ROLE_REFERENCE}

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


class RubricMaterialError(RuntimeError):
    pass


def _extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def extract_docx_text(data: bytes) -> str:
    """提取 .docx 中按段落组织的文字，表格单元格也会包含在段落顺序中。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise RubricMaterialError("Word 文件无法解析，请上传有效的 .docx 文件") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RubricMaterialError(f"Word 文档内容损坏：{exc}") from exc

    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", _NS):
        parts: list[str] = []
        for node in paragraph.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "t" and node.text:
                parts.append(node.text)
            elif local == "tab":
                parts.append("\t")
            elif local in {"br", "cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    result = "\n".join(paragraphs).strip()
    if not result:
        raise RubricMaterialError("Word 文件中没有可读取的文字")
    return result


def _validate_material(role: str, filename: str, data: bytes) -> None:
    if role not in ATTACHMENT_ROLES:
        raise RubricMaterialError("材料类型不正确")
    if not data:
        raise RubricMaterialError("上传文件为空")
    if len(data) > settings.max_upload_bytes:
        raise RubricMaterialError(f"文件超过 {settings.max_upload_mb}MB 上限")
    ext = _extension(filename)
    if role == ROLE_CRITERIA:
        if ext != ".docx" or not data.startswith(b"PK"):
            raise RubricMaterialError("评分标准必须是有效的 .docx Word 文件")
    elif ext != ".pdf" or not data.startswith(b"%PDF-"):
        raise RubricMaterialError("原题与答案材料必须是有效的 PDF 文件")


def prepare_attachment(
    db: Session,
    rubric: Rubric,
    *,
    role: str,
    filename: str,
    data: bytes,
    content_type: str | None,
) -> tuple[RubricAttachment, list[str]]:
    """保存并解析一份材料；返回记录和被替换的旧对象 key。"""
    _validate_material(role, filename, data)
    clean_name = safe_filename(filename)
    checksum = sha256_hex(data)
    storage = get_storage()
    row = (
        db.execute(
            select(RubricAttachment).where(
                RubricAttachment.rubric_id == rubric.id,
                RubricAttachment.role == role,
            )
        )
        .scalars()
        .first()
    )
    rubric_folder = safe_filename(rubric.name)
    role_folder = "word" if role == ROLE_CRITERIA else "pdf"
    base = f"rubrics/{rubric_folder}/{role_folder}"
    object_key = f"{base}/{clean_name}"
    storage.put_bytes(object_key, data, content_type)

    page_keys: list[str] = []
    extracted_text: str | None = None
    try:
        if role == ROLE_CRITERIA:
            extracted_text = extract_docx_text(data)
        else:
            bundle = render_pdf_pages(
                data,
                stem=Path(clean_name).stem,
                max_edge=settings.llm_image_max_edge,
            )
            extracted_text = bundle.text
            for image in bundle.images:
                key = f"{base}/pages/{Path(clean_name).stem}-{image.seq:03d}.png"
                storage.put_bytes(key, image.data, image.content_type)
                page_keys.append(key)
            if not page_keys:
                raise RubricMaterialError("PDF 中没有可渲染的页面")
    except AnswerFileError as exc:
        storage.delete(object_key)
        for key in page_keys:
            storage.delete(key)
        raise RubricMaterialError(str(exc)) from exc
    except Exception:
        storage.delete(object_key)
        for key in page_keys:
            storage.delete(key)
        raise

    old_keys: list[str] = []
    if row is None:
        row = RubricAttachment(rubric_id=rubric.id, role=role)
        db.add(row)
    else:
        old_keys.append(row.object_key)
        old_keys.extend(json.loads(row.page_keys_json or "[]"))
    current_keys = {object_key, *page_keys}
    old_keys = [key for key in dict.fromkeys(old_keys) if key and key not in current_keys]

    row.filename = clean_name
    row.object_key = object_key
    row.content_type = content_type
    row.size_bytes = len(data)
    row.checksum = checksum
    row.extracted_text = extracted_text
    row.page_keys_json = json.dumps(page_keys, ensure_ascii=False)
    db.flush()
    return row, old_keys


def page_keys(row: RubricAttachment) -> list[str]:
    try:
        values = json.loads(row.page_keys_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in values if item]


def delete_files(row: RubricAttachment) -> None:
    storage = get_storage()
    for key in [row.object_key, *page_keys(row)]:
        if key:
            storage.delete(key)


def delete_object_keys(keys: list[str]) -> None:
    storage = get_storage()
    for key in keys:
        if key:
            storage.delete(key)
