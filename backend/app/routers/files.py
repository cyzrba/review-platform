"""文件下载：统一走服务端代理，前端不必直连 MinIO。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from urllib.parse import quote

from ..schemas import FileUrlOut
from ..storage import get_storage
from ..utils import guess_content_type

router = APIRouter(tags=["文件"])


@router.get("/files/download", summary="下载 / 预览文件（按 object key）")
def download(key: str = Query(..., description="对象存储 key")) -> Response:
    storage = get_storage()
    try:
        payload = storage.get_bytes(key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"文件不存在或存储不可用：{exc}") from exc
    filename = key.rstrip("/").split("/")[-1] or "file"
    return Response(
        content=payload,
        media_type=guess_content_type(filename),
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/files/url", response_model=FileUrlOut, summary="获取临时直链（MinIO 预签名）")
def file_url(
    key: str = Query(..., description="对象存储 key"),
    expires: int = Query(3600, ge=60, le=86400),
) -> FileUrlOut:
    storage = get_storage()
    url = storage.presigned_url(key, expires)
    if url is None:
        # 本地磁盘后端没有预签名概念，退化成服务端代理地址
        url = f"/api/files/download?key={quote(key, safe='')}"
    return FileUrlOut(object_key=key, url=url, expires_in=expires if "http" in url else None)
