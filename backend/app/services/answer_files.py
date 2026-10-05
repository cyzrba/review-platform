"""把学生提交拆成「可以喂给 AI 的作答图片」。

作业基本都是学生拍照上传，i学习 导出的「完整答题记录」是一个 PDF：
里面既有题面图（老师发的题目截图），也有学生手写答案的照片，还有题号和得分这类文字。
这里用 PyMuPDF 按页顺序把内嵌图片抠出来，同时顺手把文字抽出来当上下文。

如果导出用的是「仅提交附件」，学生文件本来就是 jpg / png，那它自己就是一张作答图片。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# i学习 的「答题记录」导出成 .doc 时，其实是 Word 2003 XML（WordprocessingML），
# 学生的作答照片用 base64 塞在 <w:binData> 里。
_BINDATA = re.compile(r"<w:binData[^>]*>(.*?)</w:binData>", re.S)
_WORD_TEXT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)

_CONTENT_TYPE_BY_EXT = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


class AnswerFileError(RuntimeError):
    """提交文件无法解析。"""


@dataclass(slots=True)
class AnswerImage:
    seq: int
    filename: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class AnswerBundle:
    images: list[AnswerImage] = field(default_factory=list)
    text: str | None = None
    note: str = ""


def _extension(filename: str) -> str:
    lower = (filename or "").lower()
    return "." + lower.rsplit(".", 1)[-1] if "." in lower else ""


def prepare_for_llm(data: bytes, *, max_edge: int = 1600, quality: int = 85) -> tuple[bytes, str]:
    """把要发给模型的图片压一下：手机拍的作答照片动辄 3000×4000 / 1.7MB，
    压到长边 1600 字迹照样清楚，费用能降一大截。顺手按 EXIF 把方向摆正
    （手机竖拍的照片不摆正，模型会看到一张躺着的图）。
    """
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError:  # 没装 Pillow 也能跑，只是不压缩
        return data, "image/jpeg"

    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            longest = max(image.size)
            if longest > max_edge:
                ratio = max_edge / longest
                image = image.resize(
                    (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                    Image.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("图片压缩失败，按原图发送：%s", exc)
        return data, "image/jpeg"


def extract_from_pdf(data: bytes, stem: str = "answer") -> AnswerBundle:
    """从 PDF 里按页顺序抠图 + 抽文字。"""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 运行环境没装时给出明确提示
        raise AnswerFileError("没装 PyMuPDF，无法从 PDF 里提取作答图片（uv pip install pymupdf）") from exc

    bundle = AnswerBundle()
    texts: list[str] = []
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise AnswerFileError(f"PDF 打不开：{exc}") from exc

    try:
        for page_index in range(document.page_count):
            page = document[page_index]
            try:
                page_text = page.get_text().strip()
            except Exception:  # noqa: BLE001
                page_text = ""
            if page_text:
                texts.append(page_text)

            for image_index, info in enumerate(page.get_images(full=True), start=1):
                xref = info[0]
                try:
                    raw = document.extract_image(xref)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("第 %s 页第 %s 张图提取失败：%s", page_index + 1, image_index, exc)
                    continue
                payload = raw.get("image") or b""
                if not payload:
                    continue
                ext = (raw.get("ext") or "png").lower()
                bundle.images.append(
                    AnswerImage(
                        seq=len(bundle.images) + 1,
                        filename=f"{stem}-p{page_index + 1}-{image_index}.{ext}",
                        content_type=_CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream"),
                        data=payload,
                    )
                )
    finally:
        document.close()

    bundle.text = "\n".join(texts) or None
    if not bundle.images:
        bundle.note = "PDF 里没有内嵌图片（可能是纯文字作答）"
    return bundle


def render_pdf_pages(
    data: bytes, stem: str = "answer", *, max_edge: int = 1700
) -> AnswerBundle:
    """把 PDF 的每一页渲染成一张图片。

    作业是学生拍照上传，PDF 里内嵌的就是照片，直接用 `extract_from_pdf` 抠图即可。
    实验报告不一样：学生是在电脑上排版出来的，正文是矢量文字、只有图表是内嵌图片，
    只抠内嵌图会丢掉绝大部分内容，所以这里整页渲染 —— 模型能看到完整的报告。
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise AnswerFileError("没装 PyMuPDF，无法把 PDF 渲染成图片（uv pip install pymupdf）") from exc

    bundle = AnswerBundle()
    texts: list[str] = []
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise AnswerFileError(f"PDF 打不开：{exc}") from exc

    try:
        for page_index in range(document.page_count):
            page = document[page_index]
            try:
                page_text = page.get_text().strip()
            except Exception:  # noqa: BLE001
                page_text = ""
            if page_text:
                texts.append(page_text)
            try:
                rect = page.rect
                longest = max(rect.width, rect.height) or 1.0
                zoom = max(1.0, min(4.0, max_edge / longest))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                payload = pixmap.tobytes("png")
            except Exception as exc:  # noqa: BLE001
                logger.warning("第 %s 页渲染失败：%s", page_index + 1, exc)
                continue
            bundle.images.append(
                AnswerImage(
                    seq=len(bundle.images) + 1,
                    filename=f"{stem}-p{page_index + 1}.png",
                    content_type="image/png",
                    data=payload,
                )
            )
    finally:
        document.close()

    bundle.text = "\n".join(texts) or None
    if not bundle.images:
        bundle.note = "PDF 里没有可渲染的页面"
    return bundle


def extract_lab_report(filename: str, data: bytes) -> AnswerBundle:
    """实验报告：PDF 整页渲染成图；别的格式退回按内嵌图片处理。"""
    if _extension(filename) == ".pdf":
        return render_pdf_pages(data, stem=filename.rsplit(".", 1)[0])
    return extract_answer_files(filename, data)


def extract_answer_files(filename: str, data: bytes) -> AnswerBundle:
    """按文件类型把提交拆成作答图片。"""
    ext = _extension(filename)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    if ext == ".pdf":
        return extract_from_pdf(data, stem=stem)

    if ext in {".doc", ".docx"} or data[:5] in (b"<?xml", b"\xef\xbb\xbf<"):
        # i学习 导出的 .doc 其实是 Word 2003 XML，图片是 base64 内嵌的
        text = data.decode("utf-8-sig", errors="replace")
        if "<w:wordDocument" in text or "<w:binData" in text:
            return extract_from_word_xml(text, stem=stem, filename=filename)

    if ext in IMAGE_EXTENSIONS:
        content_type = _CONTENT_TYPE_BY_EXT.get(ext.lstrip("."), "image/jpeg")
        return AnswerBundle(
            images=[
                AnswerImage(
                    seq=1,
                    filename=filename,
                    content_type=content_type,
                    data=data,
                )
            ]
        )

    raise AnswerFileError(f"暂不支持从 {ext or '未知类型'} 里提取作答图片")


def _sniff_image(payload: bytes) -> tuple[str, str] | None:
    """按文件头认出图片类型，认不出就不是图片（比如内嵌的 OLE 对象）。"""
    if payload[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp", "webp"
    if payload[:2] == b"BM":
        return "image/bmp", "bmp"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    return None


def extract_from_word_xml(text: str, *, stem: str, filename: str) -> AnswerBundle:
    """从 Word 2003 XML（伪 .doc）里抠出 base64 内嵌的作答图片和文字。"""
    bundle = AnswerBundle()
    for index, raw in enumerate(_BINDATA.findall(text), start=1):
        try:
            payload = base64.b64decode(re.sub(r"\s+", "", raw), validate=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 里第 %s 段 base64 解不开：%s", filename, index, exc)
            continue
        sniffed = _sniff_image(payload)
        if sniffed is None:
            continue
        content_type, ext = sniffed
        bundle.images.append(
            AnswerImage(
                seq=len(bundle.images) + 1,
                filename=f"{stem}-word-{index}.{ext}",
                content_type=content_type,
                data=payload,
            )
        )

    pieces = [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", item)).strip()
        for item in _WORD_TEXT.findall(text)
    ]
    body = " ".join(piece for piece in pieces if piece)
    # i学习 生成的记录里，base64 图片数据也会混在文字节点里，这里清掉
    body = re.sub(r"[A-Za-z0-9+/=]{80,}", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    bundle.text = body or None
    if not bundle.images:
        bundle.note = "Word 记录里没有内嵌图片（可能是纯文字作答）"
    return bundle
