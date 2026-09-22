"""压缩包展开与学生匹配。

两类任务的文件结构不一样：

实验报告（lab_report）
    上传的压缩包里是「每个学生一个压缩包」，名为 ``学生名字_学号.zip``，
    里面是 ``学生名字_学号.pdf``。

作业（homework）
    上传的压缩包里既有压缩包也有 Word 文件，只关心 Word 文件，
    文件名为 ``院系-专业-班级名称-学号-学生名称.docx``。

两者都靠文件名锁定学生，所以这里做两件事：把嵌套压缩包展开成叶子文件，
再按「学号 / 姓名」给文件名打分匹配。

另外有个老问题：Windows 打的 zip 常把中文名按 GBK 存，而 zip 规范只标了
「不是 UTF-8」，所以需要按 cp437 解回来再按 gbk 解码。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from ..config import settings

# 压缩包里的噪音文件
_IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "__macosx"}
_IGNORED_PREFIXES = ("~$", "._")
_IGNORED_SUFFIXES = (".tmp",)

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_CJK = r"\u4e00-\u9fff"

MAX_NEST_DEPTH = 3  # 压缩包里还能套几层压缩包
MAX_ISSUES = 40  # 返回给前端的异常条目上限，避免刷屏


class ArchiveError(ValueError):
    """压缩包无法解析。"""


# --------------------------------------------------------------------------- #
# 文件类型规则
# --------------------------------------------------------------------------- #
KIND_RULES: dict[str, dict] = {
    "lab_report": {
        "label": "实验报告",
        "preferred": {".pdf"},
        "accepted": {".pdf", ".doc", ".docx", ".txt", ".md"},
        "expect": "每个学生一个压缩包（学生名字_学号.zip），里面是 学生名字_学号.pdf",
    },
    "homework": {
        "label": "作业",
        "preferred": {".docx"},
        "accepted": {".doc", ".docx"},
        "expect": "压缩包里的 Word 文件，文件名形如 院系-专业-班级名称-学号-学生名称.docx",
    },
}


def kind_rule(kind: str) -> dict:
    return KIND_RULES.get(kind, KIND_RULES["homework"])


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LeafFile:
    """展开嵌套压缩包之后得到的一个真实文件。"""

    name: str  # 文件名
    path: str  # 在所属压缩包内的相对路径
    parents: list[str]  # 外层压缩包名，由外到内
    data: bytes
    size: int

    @property
    def hints(self) -> list[str]:
        """可用于识别学生的名字串，按可信度从高到低。"""
        return [self.name, *reversed(self.parents), self.path]

    @property
    def origin(self) -> str:
        return " / ".join([*self.parents, self.path]) if self.parents else self.path


@dataclass(slots=True)
class Issue:
    filename: str
    reason: str


@dataclass(slots=True)
class MatchResult:
    student_id: int | None
    student_no: str | None
    name: str | None
    score: int
    reason: str
    ambiguous: bool = False


@dataclass(slots=True)
class StudentRef:
    """参与匹配的学生。class_name 用于打破「同学号跨班级」的平局。"""

    id: int
    student_no: str
    name: str
    class_name: str = ""


@dataclass(slots=True)
class MatchedFile:
    student_id: int
    student_no: str
    name: str
    leaf: LeafFile
    reason: str


@dataclass
class ScanResult:
    leaves: list[LeafFile] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 压缩包展开
# --------------------------------------------------------------------------- #
def decode_zip_name(raw_name: str, flag_bits: int) -> str:
    """zip 中文名解码：标了 UTF-8 就直接用，否则按 cp437 -> gbk 还原。"""
    if flag_bits & 0x800:
        return raw_name
    for encoding in ("gbk", "utf-8"):
        try:
            return raw_name.encode("cp437").decode(encoding)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return raw_name


def _is_noise(path: str) -> bool:
    parts = path.split("/")
    basename = parts[-1].lower()
    if any(part.lower() == "__macosx" for part in parts[:-1]):
        return True
    if basename in _IGNORED_NAMES:
        return True
    if basename.startswith(_IGNORED_PREFIXES):
        return True
    return basename.endswith(_IGNORED_SUFFIXES)


def is_zip(filename: str) -> bool:
    return (filename or "").lower().endswith(".zip")


def _extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def scan_uploads(
    uploads: list[tuple[str, bytes]], *, max_total_bytes: int | None = None
) -> ScanResult:
    """把一批上传文件（含嵌套压缩包）展开成叶子文件列表。"""
    budget = {"total": 0}
    limit = max_total_bytes or settings.max_upload_bytes * 4
    result = ScanResult()

    for filename, data in uploads:
        if is_zip(filename):
            try:
                _scan_zip(filename, data, [], result, budget, limit)
            except ArchiveError as exc:
                result.issues.append(Issue(filename, str(exc)))
        else:
            result.leaves.append(LeafFile(filename, filename, [], data, len(data)))
    return result


def _scan_zip(
    zip_name: str,
    data: bytes,
    parents: list[str],
    result: ScanResult,
    budget: dict[str, int],
    limit: int,
) -> None:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"{zip_name} 不是有效的 zip 压缩包") from exc

    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = decode_zip_name(info.filename, info.flag_bits).replace("\\", "/")
            if _is_noise(path) or path.startswith("/"):
                continue
            if info.file_size == 0:
                continue

            budget["total"] += info.file_size
            if budget["total"] > limit:
                raise ArchiveError(f"解压后体积超过上限（{limit // 1024 // 1024}MB），请拆分后上传")

            payload = archive.read(info)
            basename = path.split("/")[-1]

            if is_zip(basename):
                if len(parents) + 1 >= MAX_NEST_DEPTH:
                    result.issues.append(
                        Issue(" / ".join([*parents, path]), "压缩包嵌套层级过深，已跳过")
                    )
                    continue
                try:
                    _scan_zip(basename, payload, [*parents, basename], result, budget, limit)
                except ArchiveError as exc:
                    result.issues.append(Issue(" / ".join([*parents, path]), str(exc)))
                continue

            result.leaves.append(LeafFile(basename, path, list(parents), payload, len(payload)))


def select_documents(leaves: list[LeafFile], kind: str) -> tuple[list[LeafFile], list[Issue]]:
    """按任务类型挑出真正参与评审的文件，其余记为跳过。"""
    rule = kind_rule(kind)
    accepted: set[str] = rule["accepted"]
    preferred: set[str] = rule["preferred"]

    documents = [leaf for leaf in leaves if _extension(leaf.name) in accepted]
    # 优先用更合适的格式（作业优先 .docx，实验报告优先 .pdf）
    documents.sort(key=lambda leaf: 0 if _extension(leaf.name) in preferred else 1)

    skipped = [
        Issue(leaf.origin, f"{rule['label']}只评审 {'/'.join(sorted(accepted))} 文件，已跳过")
        for leaf in leaves
        if _extension(leaf.name) not in accepted
    ]
    return documents, skipped


# --------------------------------------------------------------------------- #
# 学生匹配
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_SPLIT.split(text) if token}


def _contains_student_no(text: str, student_no: str) -> bool:
    """学号出现在文本里，且前后都不是数字（避免 2021001 命中 20210012）。"""
    if len(student_no) < 4:
        return False
    return re.search(rf"(?<!\d){re.escape(student_no)}(?!\d)", text) is not None


def _contains_name(text: str, name: str) -> bool:
    """姓名出现在文本里，且前后都不是汉字（避免 张三 命中 张三丰）。"""
    if len(name) < 2:
        return False
    return re.search(rf"(?<![{_CJK}]){re.escape(name)}(?![{_CJK}])", text) is not None


def score_candidate(hint: str, student_no: str, name: str) -> tuple[int, str]:
    """给「这个文件属于这个学生」的可信度打分（0-100）。"""
    stem = hint.rsplit(".", 1)[0]
    tokens = _tokens(stem)
    no = student_no.strip().lower()
    nm = name.strip()
    stem_lower = stem.lower()

    has_no_token = bool(no) and no in tokens
    has_name_token = bool(nm) and nm.lower() in tokens

    if has_no_token and has_name_token:
        return 100, f"文件名同时包含学号 {student_no} 和姓名 {name}"
    if has_no_token:
        return 94, f"文件名含学号 {student_no}"
    if has_name_token:
        return 88, f"文件名含姓名 {name}"
    if _contains_student_no(stem_lower, no):
        return 80, f"文件名中出现学号 {student_no}"
    if _contains_name(stem, nm):
        return 72, f"文件名中出现姓名 {name}"
    return 0, ""


def match_student(
    hints: list[str],
    students: list[StudentRef],
    *,
    strong_threshold: int = 88,
    weak_threshold: int = 72,
) -> MatchResult:
    """把文件名（或外层压缩包名）匹配到学生。

    同一学号跨班级重复时（比如重修），用文件名里出现的班级名做二次判定。
    """
    candidates: list[tuple[int, StudentRef, str]] = []  # score, student, reason
    for student in students:
        best_score, best_reason = 0, ""
        for hint in hints:
            if not hint:
                continue
            score, reason = score_candidate(hint, student.student_no, student.name)
            if score > best_score:
                best_score, best_reason = score, reason
        if best_score >= weak_threshold:
            candidates.append((best_score, student, best_reason))

    def describe(entries: list[tuple[int, StudentRef, str]]) -> str:
        return "、".join(f"{item[1].name}({item[1].student_no})" for item in entries[:3])

    if not candidates:
        # 兜底提示：姓名出现了，但被别的汉字包住（比如「王五的实验报告」），
        # 这种不敢直接采用（会把「李四」错配给「李四光」），但值得提示教师改文件名。
        loose: list[str] = []
        for student in students:
            if len(student.name) < 2:
                continue
            if any(student.name in hint for hint in hints if hint):
                loose.append(f"{student.name}({student.student_no})")
        if loose:
            return MatchResult(
                None,
                None,
                None,
                0,
                f"文件名里出现 {'、'.join(loose[:3])} 的字样，但没有独立的学号或姓名段"
                "（建议命名成 姓名_学号.pdf），未采用",
            )
        return MatchResult(None, None, None, 0, "文件名里找不到学号或姓名")

    candidates.sort(key=lambda item: item[0], reverse=True)
    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None

    # 分数并列（典型是同学号跨班级，或同名同姓）时，用文件名里的班级名再判一次
    if runner_up is not None and runner_up[0] == top[0]:
        top_hit = _mentions_class(hints, top[1].class_name)
        runner_hit = _mentions_class(hints, runner_up[1].class_name)
        if top_hit != runner_hit:
            winner = top if top_hit else runner_up
            return MatchResult(
                winner[1].id,
                winner[1].student_no,
                winner[1].name,
                winner[0],
                f"{winner[2]}；并用班级名 {winner[1].class_name} 区分",
            )
        return MatchResult(
            None, None, None, top[0], f"可能对应多个学生：{describe(candidates)}", ambiguous=True
        )

    if top[0] >= strong_threshold:
        return MatchResult(top[1].id, top[1].student_no, top[1].name, top[0], top[2])

    if runner_up is not None and top[0] - runner_up[0] < 8:
        return MatchResult(
            None, None, None, top[0], f"可能对应多个学生：{describe(candidates)}", ambiguous=True
        )

    return MatchResult(top[1].id, top[1].student_no, top[1].name, top[0], top[2])


def _mentions_class(hints: list[str], class_name: str) -> bool:
    """文件名里是否出现了这个班级名。"""
    if not class_name:
        return False
    for hint in hints:
        if not hint:
            continue
        stem = hint.rsplit(".", 1)[0]
        if class_name in _tokens(stem) or class_name in stem:
            return True
    return False


def assign_students(
    documents: list[LeafFile], students: list[StudentRef]
) -> tuple[list[MatchedFile], list[Issue]]:
    """给每个文件找学生；一个学生只保留一份最合适的文件。"""
    if not students:
        return [], [Issue("(全部文件)", "该班级还没有学生，请先导入名单")]

    matched: list[MatchedFile] = []
    issues: list[Issue] = []
    used: dict[int, MatchedFile] = {}

    for leaf in documents:
        result = match_student(leaf.hints, students)
        if result.student_id is None:
            issues.append(Issue(leaf.origin, result.reason))
            continue
        previous = used.get(result.student_id)
        if previous is not None:
            issues.append(
                Issue(leaf.origin, f"{result.name} 已用 {previous.leaf.name} 评审，本文件已跳过")
            )
            continue
        entry = MatchedFile(
            student_id=result.student_id,
            student_no=result.student_no or "",
            name=result.name or "",
            leaf=leaf,
            reason=result.reason,
        )
        used[result.student_id] = entry
        matched.append(entry)

    return matched, issues


def trim_issues(issues: list[Issue], limit: int = MAX_ISSUES) -> list[Issue]:
    """异常条目太多时只留前若干条，并给一条汇总。"""
    if len(issues) <= limit:
        return issues
    return [*issues[:limit], Issue("…", f"还有 {len(issues) - limit} 条同类问题未列出")]
