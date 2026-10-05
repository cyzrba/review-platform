"""从提交文件里抽取纯文本，供 AI 评审使用。

纯文本 / Excel 直接读；PDF 交给 PyMuPDF；i学习 导出的「答题记录 .doc」其实是
Word 2003 XML（WordprocessingML），也从这里抽文字。
"""

from __future__ import annotations

import io
import re

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
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover
            raise DocumentParseError("没装 PyMuPDF，PDF 文本抽取不可用") from exc
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001
            raise DocumentParseError(f"PDF 打不开：{exc}") from exc
        try:
            return _truncate(
                "\n".join(page.get_text().strip() for page in document)
            )
        finally:
            document.close()

    if ext in {".docx", ".doc"}:
        # i学习 导出的 .doc 是 Word 2003 XML，文字在 <w:t> 里
        try:
            return _truncate(_word_xml_text(data))
        except DocumentParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DocumentParseError(f"Word 文本抽取失败：{exc}") from exc

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


_WORD_TEXT_RUN = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
_LONG_BASE64 = re.compile(r"[A-Za-z0-9+/=]{80,}")


def _word_xml_text(data: bytes) -> str:
    """从 Word 2003 XML（i学习 的「答题记录 .doc」）里抽文字。

    图片是 base64 内嵌在 <w:binData> 里的，会混进文字节点，这里把长 base64 串清掉。
    """
    text = data.decode("utf-8-sig", errors="replace")
    if "<w:wordDocument" not in text and "<w:binData" not in text:
        raise DocumentParseError("这个 .doc 不是 Word 2003 XML，解析不了（建议用 .docx）")
    pieces = [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", item)).strip()
        for item in _WORD_TEXT_RUN.findall(text)
    ]
    body = _LONG_BASE64.sub(" ", " ".join(piece for piece in pieces if piece))
    return re.sub(r"\s+", " ", body).strip()
