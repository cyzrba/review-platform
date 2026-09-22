"""从提交文件里抽取纯文本，供 AI 评审使用。

目前实现了纯文本 / Excel 类文件；PDF、Word 的解析留了明确的接入点，
装上 pdfplumber / python-docx 后把对应分支补上即可。
"""

from __future__ import annotations

import io

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".yml", ".yaml", ".log",
    ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".js", ".ts", ".sql",
    ".go", ".rs", ".rb", ".php", ".sh", ".m", ".r", ".ipynb", ".html", ".css",
}

MAX_CHARS = 60_000  # 送进模型前的截断长度，避免 prompt 爆炸


class DocumentParseError(ValueError):
    """无法抽取文本。"""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(filename: str, content_type: str | None, data: bytes) -> str:
    """返回可读文本；不支持的格式抛 DocumentParseError。"""
    lower = (filename or "").lower()
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""

    if ext in TEXT_EXTENSIONS or (content_type or "").startswith("text/"):
        return _truncate(_decode(data))

    if ext == ".pdf":
        # TODO(AI 评审): 接入 pdfplumber
        #   import pdfplumber, io
        #   with pdfplumber.open(io.BytesIO(data)) as pdf:
        #       return "\n".join(page.extract_text() or "" for page in pdf.pages)
        raise DocumentParseError("PDF 文本抽取尚未接入（见 services/document.py 的 TODO）")

    if ext in {".docx", ".doc"}:
        # TODO(AI 评审): 接入 python-docx
        #   import docx
        #   document = docx.Document(io.BytesIO(data))
        #   return "\n".join(p.text for p in document.paragraphs)
        raise DocumentParseError("Word 文本抽取尚未接入（见 services/document.py 的 TODO）")

    if ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in wb.worksheets:
            lines.append(f"# sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell) for cell in row]
                if any(cells):
                    lines.append("\t".join(cells))
        wb.close()
        return _truncate("\n".join(lines))

    raise DocumentParseError(f"暂不支持的文件类型：{ext or content_type or '未知'}")


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n...[内容过长，已截断，共 {len(text)} 字]"
