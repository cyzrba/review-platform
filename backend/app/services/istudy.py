"""深职 i学习（istudy.szpu.edu.cn）名单抓取。

登录态来自本机 Edge 的调试端口（CDP）：浏览器自己把 Cookie 交给我们，
不需要读取浏览器的 Cookie 数据库，因此不涉及解密，也不受 App-Bound Encryption 影响。
拿到 Cookie 之后，抓取全部走普通 HTTP 请求。

抓取链路（教师视角：我教的课 → 课程 → 管理 → 班级 → 导出学生名单）：
    /wfw/courselist/coursegroupdata      我教的课
    /courselist/opencoursenewfy          进入课程，拿 clazzid / cfid
    /mooc2-ans/tcm/clazz-manage          班级列表
    /mooc2-ans/tcm/clazz-student         班级学生页（含 exportEnc）
    {importExportUrl}/export/personexcel 导出学生名单 Excel

注意：导出接口的 schoolId 必须传课程的 cfid（页面上的 cookieFid 隐藏字段是空的），
否则服务端会返回一个写着「enc验证失败」的 Excel。
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

import httpx
import websockets

from ..config import settings
from .importer import parse_enrollment_year, parse_joined_at

logger = logging.getLogger(__name__)

ISTUDY_BASE = "https://istudy.szpu.edu.cn"
MOOC_BASE = "https://mooc.istudy.szpu.edu.cn"
COURSE_LIST_API = f"{ISTUDY_BASE}/wfw/courselist/coursegroupdata"
COURSE_ENTRY = f"{ISTUDY_BASE}/courselist/opencoursenewfy"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# 导出学生名单 Excel 的表头 -> 平台字段
ROSTER_COLUMNS = {
    "学号/工号": "student_no",
    "学号": "student_no",
    "工号": "student_no",
    "姓名": "name",
    "院系": "department",
    "专业": "major",
    "班级": "class_name",
    "加入时间": "joined_at",
    "入学年份": "enrollment_year",
}


class IstudyError(RuntimeError):
    """抓取失败（浏览器没开、没登录、接口变化等）。"""


def _hidden(html: str, element_id: str) -> str:
    """读取隐藏 input 的 value，兼容属性顺序。"""
    for tag in re.findall(r"<input[^>]*>", html):
        if re.search(r'id="' + re.escape(element_id) + r'"', tag):
            m = re.search(r'value="([^"]*)"', tag)
            return m.group(1) if m else ""
    return ""


async def _cdp_version(port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise IstudyError(
            "连不上 i学习 浏览器（调试端口 %d）。请先双击「i学习浏览器」启动器并登录。" % port
        ) from exc


async def _cdp_cookies(port: int) -> list[dict[str, Any]]:
    version = await _cdp_version(port)
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        raise IstudyError("调试端口没有返回 WebSocket 地址")
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1:
                break
    if "error" in message:
        raise IstudyError(f"读取浏览器 Cookie 失败：{message['error']}")
    return message["result"]["cookies"]


def parse_roster_xlsx(data: bytes) -> list[dict[str, Any]]:
    """把 i学习 导出的「班级名册」Excel 解析成标准名单行。"""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        if sheet is None:
            return []
        rows = sheet.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return []
        fields = [ROSTER_COLUMNS.get(str(cell).strip(), "") for cell in header]
        out: list[dict[str, Any]] = []
        for raw in rows:
            record: dict[str, Any] = {}
            for index, field in enumerate(fields):
                if not field or index >= len(raw):
                    continue
                record[field] = raw[index]
            if not any(str(value or "").strip() for value in record.values()):
                continue
            record["student_no"] = str(record.get("student_no") or "").strip()
            record["name"] = str(record.get("name") or "").strip()
            record["joined_at"] = parse_joined_at(record.get("joined_at"))
            record["enrollment_year"] = parse_enrollment_year(record.get("enrollment_year"))
            out.append(record)
        return out
    finally:
        workbook.close()


class IstudyClient:
    """借用浏览器登录态的 i学习 客户端。"""

    def __init__(self, port: int | None = None, timeout: float = 40.0) -> None:
        self.port = port or settings.istudy_cdp_port
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.browser = ""

    async def __aenter__(self) -> "IstudyClient":
        version = await _cdp_version(self.port)
        self.browser = str(version.get("Browser") or "")
        cookies = await _cdp_cookies(self.port)
        client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, trust_env=False)
        client.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        for cookie in cookies:
            if "szpu.edu.cn" not in str(cookie.get("domain", "")):
                continue
            try:
                client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie.get("path", "/"),
                )
            except Exception:  # noqa: BLE001
                continue
        self._client = client
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise IstudyError("客户端还没初始化")
        return self._client

    # ------------------------------------------------------------------ #
    async def logged_in(self) -> bool:
        """顺便验证登录态（未登录时门户页只有登录按钮）。"""
        resp = await self.client.get(f"{ISTUDY_BASE}/portal")
        text = resp.text
        return "退出" in text or 'goPassport2Login()' not in text

    async def list_courses(self) -> list[dict[str, str]]:
        """我教的课。"""
        resp = await self.client.get(
            COURSE_LIST_API,
            params={
                "sectionId": 0,
                "semesterNum": "",
                "coursename": "",
                "searchSectionId": 0,
                "searchSemesterNum": "",
                "coursesource": 0,
                "label": "szpt",
            },
        )
        resp.raise_for_status()
        courses: list[dict[str, str]] = []
        for tag in re.findall(r'<div class="course_item[^>]*>', resp.text):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
            cid = attrs.get("cid") or ""
            cpi = attrs.get("cpi") or ""
            if cid and cpi:
                courses.append(
                    {"cid": cid, "cpi": cpi, "name": attrs.get("cname") or "(未命名课程)"}
                )
        return courses

    async def _course_handles(self, cid: str, cpi: str) -> dict[str, str]:
        resp = await self.client.get(f"{COURSE_ENTRY}?courseId={cid}&cpi={cpi}")
        resp.raise_for_status()
        handles = {
            key: _hidden(resp.text, key)
            for key in ("clazzid", "cpi", "cfid", "fid", "userId")
        }
        if not handles.get("clazzid"):
            raise IstudyError("没能进入课程（课程信息可能有变化，或登录态已失效）")
        handles["school_id"] = handles.get("cfid") or handles.get("fid") or ""
        handles["cpi"] = handles.get("cpi") or cpi
        return handles

    async def list_classes(self, cid: str, clazzid: str, cpi: str) -> list[dict[str, str]]:
        resp = await self.client.get(
            f"{MOOC_BASE}/mooc2-ans/tcm/clazz-manage",
            params={"courseid": cid, "clazzid": clazzid, "v": "0", "cpi": cpi},
        )
        resp.raise_for_status()
        found = re.findall(
            r'<li class="[^"]*clazzList"[^>]*clazz-id="(\d+)"[\s\S]{0,400}?'
            r'<p class="overHidden2 clazzName">([^<]*)</p>',
            resp.text,
        )
        return [{"clazz_id": cid_, "name": name.strip()} for cid_, name in found]

    async def _class_page(self, cid: str, clazz_id: str, cpi: str) -> str:
        resp = await self.client.get(
            f"{MOOC_BASE}/mooc2-ans/tcm/clazz-student",
            params={
                "courseid": cid,
                "clazzid": clazz_id,
                "requireStu": "",
                "cpi": cpi,
                "pageNum": 1,
                "pageShowNum": "",
                "orderContent": "",
                "schoolStatus": 0,
                "v": 0,
                "order": "",
            },
        )
        resp.raise_for_status()
        return resp.text

    async def export_class_roster(
        self, cid: str, clazz_id: str, cpi: str, school_id: str
    ) -> tuple[bytes, str]:
        """导出某个班级的学生名单，返回 (文件字节, 班级名)。"""
        page = await self._class_page(cid, clazz_id, cpi)
        import_url = _hidden(page, "importExportUrl")
        export_enc = _hidden(page, "exportEnc")
        person_id = _hidden(page, "importPersonId") or cpi
        if not import_url or not export_enc:
            raise IstudyError(f"班级 {clazz_id} 的导出参数没找到（页面结构可能变了）")
        url = (
            f"{MOOC_BASE}{import_url}/export/personexcel"
            f"?courseId={cid}&order=&orderContent=&classId={clazz_id}"
            f"&personId={person_id}&schoolId={school_id}&exportEnc={export_enc}"
        )
        referer = (
            f"{MOOC_BASE}/mooc2-ans/tcm/clazz-student?courseid={cid}"
            f"&clazzid={clazz_id}&cpi={cpi}"
        )
        resp = await self.client.get(url, headers={"Referer": referer})
        if resp.content[:2] != b"PK":
            raise IstudyError(f"班级 {clazz_id} 导出失败：{resp.text[:120]}")
        return resp.content, clazz_id

    async def fetch_roster(self, cid: str, cpi: str) -> dict[str, Any]:
        """抓取某门课程下所有班级的学生名单。"""
        handles = await self._course_handles(cid, cpi)
        classes = await self.list_classes(cid, handles["clazzid"], handles["cpi"])
        rows: list[dict[str, Any]] = []
        detail: list[dict[str, Any]] = []
        for item in classes:
            try:
                content, _ = await self.export_class_roster(
                    cid, item["clazz_id"], handles["cpi"], handles["school_id"]
                )
            except IstudyError as exc:
                detail.append({"class_name": item["name"], "count": 0, "error": str(exc)})
                continue
            parsed = parse_roster_xlsx(content)
            rows.extend(parsed)
            detail.append({"class_name": item["name"], "count": len(parsed)})
        return {
            "classes": detail,
            "rows": rows,
            "student_count": len(rows),
            "school_id": handles["school_id"],
        }
