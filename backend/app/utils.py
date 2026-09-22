"""零碎工具。"""

from __future__ import annotations

import hashlib
import mimetypes
import re

_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """去掉路径分隔符与非法字符，保留中文，去掉目录部分。"""
    base = (name or "").replace("\\", "/").split("/")[-1]
    cleaned = _UNSAFE.sub("_", base).strip().strip(".")
    if not cleaned:
        cleaned = fallback
    return cleaned[:200]


def guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_archive(filename: str) -> bool:
    return (filename or "").lower().endswith(".zip")
