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

作业（我教的课 → 课程 → 作业 → 导出作业附件）：
    /mooc2-ans/work/list                 作业列表（可按班级 / 状态筛选）
    /mooc2-ans/work/mark                 某次作业的批阅页（拿 taskId，页内有班级下拉）
    /mooc2-ans/work/packWork             提交「导出作业附件」，异步打包
    /mooc2-ans/tcm/downloadcenter        下载中心，打包完成后给直链
    https://d.istudy.szpu.edu.cn/...     实际文件（换域名，不带 mooc 的 Cookie）

作业列表页的几个关键参数（都是页面 selectStatus / selectClass 拼出来的）：
    selectClassid=0 表示全部班级，否则传 clazzid；
    status=-1 全部 / 0 未开始 / 1 进行中 / 2 已结束。
另外注意：按班级筛选时列表里的 `<li id="workN">` 里的 N 是 workId，
不过滤班级时 N 才是 taskId —— 所以按班级抓完还要用批阅页换 taskId。

实验报告（我教的课 → 课程 → 实验报告）：
    这套页面在另一个 SPA 里（``/lab/#/...``），接口全在 ``/lab/api/labTemplate`` 下：
    /report/index                            报告列表，一次给全（每个报告自带 stuList）
    /report/correction/index                 某次报告的提交名单
    /report/correction/exportStuFillPdf      导出学生作答（**同步**返回 zip，不用轮询）
报告列表要带 search[0..2]（moocCourseId / reportType / status）三个条件，缺一个就 500；
导出作答不改任何状态，也不写回 i学习，拿到的只是压缩包。
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
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
WORK_LIST = f"{MOOC_BASE}/mooc2-ans/work/list"
WORK_MARK = f"{MOOC_BASE}/mooc2-ans/work/mark"
WORK_MARK_LIST = f"{MOOC_BASE}/mooc2-ans/work/mark-list"
WORK_PACK = f"{MOOC_BASE}/mooc2-ans/work/packWork"
DOWNLOAD_CENTER = f"{MOOC_BASE}/mooc2-ans/tcm/downloadcenter"
LAB_API = f"{MOOC_BASE}/lab/api/labTemplate"

_MARK_ROW = re.compile(
    r'<ul class="dataBody_td"\s+id="(\d+)"\s+createid="(\d+)"[^>]*>(.*?)</ul>', re.S
)
_MARK_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S)

# 作业状态：列表页 状态 那一栏的 data 值
WORK_STATUS_BY_CODE = {"-1": None, "0": "not_started", "1": "ongoing", "2": "ended"}

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


def _tags(html: str, tag: str) -> list[str]:
    """把某个标签的整段开标签抠出来（用于属性无序解析）。"""
    return re.findall(rf"<{tag}\b[^>]*>", html)


def _attrs(tag: str) -> dict[str, str]:
    return {key.lower(): value for key, value in re.findall(r'([\w:-]+)="([^"]*)"', tag)}


def _text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip().lstrip("\u200b")


# --------------------------------------------------------------------------- #
# 作业列表 / 批阅页 / 下载中心 的解析
# --------------------------------------------------------------------------- #
def parse_work_rows(html: str) -> list[dict[str, Any]]:
    """解析作业列表页的每一条作业。

    返回的 ``istudy_id`` 有两种含义：不筛班级时是 taskId，筛班级时是 workId
    （页面 ``viewWork`` 的第二个参数就是它）。

    ``library_id`` 是 ``viewWork`` 的第一个参数（作业题库 id）：**一次作业发给多个班时，
    这个字段是一样的**，所以它才是「这是哪一次作业」的真正标识，taskId / workId 都只是批次。
    """
    rows: list[dict[str, Any]] = []
    for block in re.split(r'<li id="work', html)[1:]:
        try:
            quote = block.index('"')
        except ValueError:
            continue
        istudy_id = block[:quote]
        if not istudy_id.isdigit():
            continue

        title = re.search(r'<h2 class="list_li_tit[^"]*"[^>]*>(.*?)</h2>', block, re.S)
        classes = re.search(r'<div class="list_class[^"]*"[^>]*title="([^"]*)"', block)
        time_text = re.search(r"<span>\s*(作答时间：[^<]*?)\s*</span>", block)
        pending = re.search(r'<em class="fs28"[^>]*>\s*(\d+)\s*</em>\s*待批', block)
        submitted = re.search(r"(\d+)\s*已交", block)
        unsubmitted = re.search(r"(\d+)\s*未交", block)
        view = re.search(r"viewWork\(\s*'([^']*)'\s*,", block)

        rows.append(
            {
                "istudy_id": istudy_id,
                "library_id": view.group(1) if view else "",
                "name": _clean_name(_text_of(title.group(1))) if title else "",
                "class_names": [
                    item.strip()
                    for item in (classes.group(1) if classes else "").split("，")
                    if item.strip()
                ],
                "time_text": _clean_name(time_text.group(1)) if time_text else "",
                "pending_count": int(pending.group(1)) if pending else 0,
                "submitted_count": int(submitted.group(1)) if submitted else 0,
                "unsubmitted_count": int(unsubmitted.group(1)) if unsubmitted else 0,
            }
        )
    return rows


def parse_work_class_options(html: str) -> list[dict[str, str]]:
    """批阅页里的班级下拉：每个班级对应的 clazzid 和 workId。"""
    options: list[dict[str, str]] = []
    for tag in _tags(html, "li"):
        if "classli" not in tag:
            continue
        attrs = _attrs(tag)
        clazzid = attrs.get("data", "")
        work_id = attrs.get("data1", "")
        if clazzid.isdigit() and work_id.isdigit():
            options.append(
                {
                    "clazzid": clazzid,
                    "work_id": work_id,
                    "name": _clean_name(attrs.get("title", "")),
                }
            )
    return options


def parse_download_items(html: str) -> list[dict[str, str]]:
    """下载中心的条目：文件名、状态、直链。"""
    from urllib.parse import unquote

    items: list[dict[str, str]] = []
    for block in re.split(r'<ul class="dataBody_td"', html)[1:]:
        head = block[: block.index(">")] if ">" in block else ""
        id_match = re.search(r'data="(\d+)"', head)
        status_match = re.search(r'data-status="(\d+)"', head)
        if not id_match:
            continue
        name = re.search(r'class="nameText">([^<]*)<', block)
        link = re.search(r'href="([^"]*)"[^>]*class="download_ic"', block)
        time_match = re.search(r"<li>(\d\d-\d\d \d\d:\d\d)</li>", block)
        items.append(
            {
                "id": id_match.group(1),
                "done": "1" if status_match and status_match.group(1) == "1" else "0",
                "name": unquote(name.group(1)).strip() if name else "",
                "url": link.group(1).replace("&amp;", "&") if link else "",
                "created_at": time_match.group(1) if time_match else "",
            }
        )
    return items


def parse_mark_list(html: str) -> tuple[list[dict[str, str]], int]:
    """解析批阅页「按人批阅」的学生列表（已交 / 未交）。

    已交的行长这样：``<ul class="dataBody_td" id="9936126" createid="18600001919017">``，
    ``createid`` 就是 i学习 的用户 id —— 只导出指定学生时 ``personIds`` 用的就是它。
    返回 (学生列表, 总页数)。
    """
    total_page = re.search(r'id="totalPage" value="(\d+)"', html)
    students: list[dict[str, str]] = []
    for answer_id, create_id, body in _MARK_ROW.findall(html):
        cells = [_text_of(item) for item in _MARK_LI.findall(body)]
        students.append(
            {
                "answer_id": answer_id,
                "istudy_user_id": create_id,
                "name": cells[1] if len(cells) > 1 else "",
                "student_no": cells[2] if len(cells) > 2 else "",
                "submitted_at": cells[3] if len(cells) > 3 else "",
                "state": cells[5] if len(cells) > 5 else "",
            }
        )
    return students, int(total_page.group(1)) if total_page else 1


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

    # ------------------------------------------------------------------ #
    # 作业（我教的课 → 课程 → 作业 → 导出作业附件）
    # ------------------------------------------------------------------ #
    async def course_context(self, cid: str, cpi: str) -> dict[str, str]:
        """进课程页，拿「作业列表」链接需要的那一串隐藏字段。"""
        resp = await self.client.get(f"{COURSE_ENTRY}?courseId={cid}&cpi={cpi}")
        resp.raise_for_status()
        keys = (
            "courseid", "clazzid", "cpi", "enc", "oldenc", "t",
            "openc", "userId", "cfid", "fid", "v",
        )
        context = {key: _hidden(resp.text, key) for key in keys}
        if not context.get("courseid"):
            raise IstudyError("进课程失败：登录态可能已失效，或课程信息有变化")
        return context

    def _work_list_url(
        self, cid: str, context: dict[str, str], select_classid: str, status: str
    ) -> str:
        """按页面 selectClass / selectStatus 拼作业列表地址。"""
        clazzid = context.get("clazzid") or context.get("courseid") or ""
        return (
            f"{WORK_LIST}?courseid={cid}&clazzid={clazzid}"
            f"&courseId={cid}&classId={clazzid}&clazzId={clazzid}"
            f"&cpi={context.get('cpi', '')}&enc={context.get('enc', '')}"
            f"&openc={context.get('openc', '')}&t={context.get('t', '')}&ut=t"
            f"&selectClassid={select_classid}&status={status}"
            f"&v={context.get('v', '')}"
        )

    async def list_works(
        self,
        cid: str,
        cpi: str,
        context: dict[str, str],
        *,
        select_classid: str = "0",
        status: str = "-1",
    ) -> list[dict[str, Any]]:
        """作业列表。``select_classid="0"`` 是全部班级，``status="-1"`` 是全部状态。"""
        referer = f"{COURSE_ENTRY}?courseId={cid}&cpi={cpi}"
        resp = await self.client.get(
            self._work_list_url(cid, context, select_classid, status),
            headers={"Referer": referer},
        )
        if resp.status_code >= 400 or "没有此页面访问权限" in resp.text:
            raise IstudyError("打开作业列表失败：登录态可能已失效，请重新登录 i学习")
        return parse_work_rows(resp.text)

    async def work_mark_context(
        self, cid: str, cpi: str, clazzid: str, work_id: str
    ) -> dict[str, Any]:
        """打开某次作业的批阅页，拿到 taskId 和这个作业覆盖的班级列表。"""
        resp = await self.client.get(
            f"{WORK_MARK}?courseid={cid}&clazzid={clazzid}&cpi={cpi}&id={work_id}"
            f"&submit=&evaluation=0&from=&ceyan=0&chapterid=&workLibraryid="
            f"&prePageSize=&prePageNum=&noBack=&topicid=0&backurl=&attachmentWorkId=",
            headers={"Referer": f"{COURSE_ENTRY}?courseId={cid}&cpi={cpi}"},
        )
        page = resp.text
        hidden = {key: _hidden(page, key) for key in ("workid", "taskId", "courseid", "clazzid")}
        if not hidden.get("workid") or not hidden.get("taskId"):
            raise IstudyError(f"批阅页没有返回作业参数（workId={work_id} clazzid={clazzid}）")
        return {
            "work_id": hidden["workid"],
            "task_id": hidden["taskId"],
            "class_options": parse_work_class_options(page),
        }

    async def request_work_export(
        self,
        cid: str,
        cpi: str,
        *,
        clazzid: str,
        work_id: str,
        task_id: str,
        content: int = 0,
        fmt: int = 1,
        scope: int = 0,
        person_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """提交「导出作业附件」。只是入队，真正的打包是异步的。

        content: 0 完整答题记录 / 1 仅提交附件 / 2 仅文档留痕批注
        fmt:     0 Word / 1 PDF
        scope:   0 本班级 / 1 作业下所有班级
        person_ids: 只导出这几个人（i学习 的用户 id，来自批阅页的 createid）；
                    传了就走 POST，而且导出范围自动变成「指定的这几个人」。
        """
        params = {
            "courseid": cid,
            "clazzid": clazzid,
            "workid": work_id,
            "type": 0,
            "uid": self.client.cookies.get("UID") or "",
            "fid": self.client.cookies.get("fid") or "0",
            "onlyattachment": content,
            "taskId": task_id,
            "isPdf": fmt,
            "packtype": 1,
            "customNameGroup": "",
            "wordCustomFormat": "",
            "personIds": "",
        }
        if person_ids:
            # 按人导出时页面发的是 POST + personIds
            params["personIds"] = ",".join(person_ids)
            resp = await self.client.post(WORK_PACK, data=params)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise IstudyError(
                    f"按人导出失败（{resp.status_code}）：{resp.text[:120]}"
                ) from exc
            if str(payload.get("status")) not in ("0", "1"):
                raise IstudyError(f"i学习 拒绝按人导出：{payload}")
            return payload
        if scope == 1:
            # 导出作业下所有班级走另一个地址
            resp = await self.client.get(
                f"{MOOC_BASE}/mooc2-ans/work/packWorkTask", params=params
            )
        else:
            resp = await self.client.get(WORK_PACK, params=params)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise IstudyError(
                f"提交导出失败（{resp.status_code}）：{resp.text[:120]}"
            ) from exc
        return payload

    async def list_submitted_students(
        self, cid: str, cpi: str, clazzid: str, work_id: str
    ) -> list[dict[str, str]]:
        """i学习 上这次作业这个班「已交」的学生名单（带 createid，可用于按人导出）。"""
        students: list[dict[str, str]] = []
        seen: set[str] = set()
        page = 1
        while page <= 50:
            resp = await self.client.get(
                WORK_MARK_LIST,
                params={
                    "courseid": cid,
                    "clazzid": clazzid,
                    "workid": work_id,
                    "cpi": cpi,
                    "submit": "true",
                    "status": "",
                    "evaluation": 0,
                    "groupId": 0,
                    "sort": 0,
                    "order": 0,
                    "from": "",
                    "topicid": 0,
                    # 注意：这个接口的分页参数就叫 pages / size，不是 pageNum / pageSize
                    # （传错了不会报错，但每页都返回第一页，结果会重复）
                    "pages": page,
                    "size": 100,
                },
            )
            if resp.status_code >= 400:
                raise IstudyError(f"读取 i学习 已交名单失败（{resp.status_code}）")
            rows, total_page = parse_mark_list(resp.text)
            fresh = [row for row in rows if row["student_no"] not in seen]
            for row in fresh:
                seen.add(row["student_no"])
            students.extend(fresh)
            if not fresh or page >= total_page:
                break
            page += 1
        return students

    async def list_download_center(self, cid: str, cpi: str) -> list[dict[str, str]]:
        """下载中心列表（导出任务是异步的，要轮询这里）。"""
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self.client.get(
                    DOWNLOAD_CENTER,
                    params={
                        "courseId": cid,
                        "pageNum": 1,
                        "cpi": cpi,
                        "order": "down",
                        "_t": int(time.time() * 1000),
                    },
                    headers={"Cache-Control": "no-cache"},
                )
                resp.raise_for_status()
                return parse_download_items(resp.text)
            except (httpx.HTTPError, OSError) as exc:
                # 轮询会跑好几分钟，偶尔会被服务端掐断连接，重试即可
                last = exc
                logger.warning("读下载中心失败（第 %s 次）：%s", attempt + 1, exc)
                await asyncio.sleep(2)
        raise IstudyError(f"读下载中心一直失败：{last}")

    async def download_export(self, url: str) -> bytes:
        """下载导出好的压缩包。

        直链在 d.istudy.szpu.edu.cn 上，跟 mooc 不是同一个域名，
        所以这里用一个干净的客户端，不带任何 Cookie。
        """
        async with httpx.AsyncClient(
            timeout=600.0, follow_redirects=True, trust_env=False
        ) as plain:
            resp = await plain.get(url)
            resp.raise_for_status()
            return resp.content

    # ------------------------------------------------------------------ #
    # 实验报告（我教的课 → 课程 → 实验报告）
    # ------------------------------------------------------------------ #
    async def lab_dept_id(self, cid: str, cpi: str) -> str:
        """实验报告接口要的 deptId / fid。

        实测课程页里的 fid（= 学校 id，1860）就能用；拿不到就退回 1860。
        """
        try:
            handles = await self.course_context(cid, cpi)
        except IstudyError:
            return "1860"
        return handles.get("cfid") or handles.get("fid") or "1860"

    async def lab_reports(self, cid: str, *, dept_id: str = "1860") -> list[dict[str, Any]]:
        """某门课下「实验报告」这一栏的全部报告（含每个报告的 stuList）。

        注意 search[1]（reportType=1，表示「实验报告」而不是模板库）和
        search[2]（status=1）都不能省，省了服务端直接 500。
        """
        resp = await self.client.get(
            f"{LAB_API}/report/index",
            params={
                "cpage": 1,
                "pageSize": 200,
                "search[0][field]": "moocCourseId",
                "search[0][val]": cid,
                "search[0][operator]": "eq",
                "search[1][field]": "reportType",
                "search[1][val]": 1,
                "search[1][operator]": "eq",
                "search[2][field]": "status",
                "search[2][val]": 1,
                "search[2][operator]": "eq",
                "deptId": dept_id,
                "fid": dept_id,
            },
        )
        if resp.status_code >= 400:
            raise IstudyError(
                f"读取实验报告列表失败（{resp.status_code}），登录态可能已失效"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise IstudyError("实验报告列表返回的不是 JSON，登录态可能已失效") from exc
        if payload.get("code") not in (1000, None):
            raise IstudyError(f"i学习 拒绝返回实验报告列表：{payload.get('msg')}")
        return list((payload.get("data") or {}).get("list") or [])

    async def lab_corrections(
        self, report_id: str | int, *, dept_id: str = "1860"
    ) -> list[dict[str, Any]]:
        """某次实验报告的提交名单（只有真正交了的人会出现在这里）。"""
        resp = await self.client.get(
            f"{LAB_API}/report/correction/index",
            params={
                "pageSize": 500,
                "cpage": 1,
                "search[0][field]": "reportId",
                "search[0][val]": report_id,
                "search[0][operator]": "eq",
                "search[1][field]": "type",
                "search[1][val]": 1,
                "search[1][operator]": "eq",
                "deptId": dept_id,
                "fid": dept_id,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        return list((payload.get("data") or {}).get("list") or [])

    async def lab_export_fill_pdf(
        self,
        report_id: str | int,
        fill_ids: list[str],
        *,
        dept_id: str = "1860",
    ) -> bytes:
        """导出指定学生的实验报告作答，**同步**返回一个 zip。

        包里是「每个学生一个压缩包（姓名_学号.zip），里面是 姓名_学号.pdf」。
        只传要抓的 fillIds，包里就只有这些人。
        """
        if not fill_ids:
            raise IstudyError("没有指定要导出的学生")
        resp = await self.client.get(
            f"{LAB_API}/report/correction/exportStuFillPdf",
            params={
                "reportId": report_id,
                "fillIds": ",".join(fill_ids),
                "deptId": dept_id,
                "fid": dept_id,
            },
            timeout=600.0,
        )
        if resp.status_code >= 400 or resp.content[:2] != b"PK":
            head = resp.content[:180].decode("utf-8", errors="replace")
            raise IstudyError(f"i学习 导出实验报告作答失败（{resp.status_code}）：{head}")
        return resp.content
