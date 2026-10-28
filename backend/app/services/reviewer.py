"""AI 评审器接口。

评审只输出「一个总分 + 一段评语」，不分维度。
真实的大模型评审逻辑先不做：这里定义好契约 + 一个 mock 实现，
让上传、入库、导出整条链路现在就能跑通；接真模型时只需要实现
`BaseReviewer.review()` 并在 `_REVIEWERS` 里注册。
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ..config import settings
from .llm_client import LLMError, ImagePart, build_messages, chat, parse_json_object

PROMPT_VERSION = "v4-rubric-documents"


@dataclass(slots=True)
class ReviewContext:
    """评审一份提交需要的全部上下文。"""

    rubric_name: str
    kind: str  # homework / lab_report
    rubric_description: str | None  # 任务说明
    criteria: str | None  # 评分细则正文
    extra_prompt: str | None
    total_score: float
    student_no: str
    student_name: str
    filename: str
    content_type: str | None
    file_bytes: bytes
    criteria_document_text: str | None = None  # 上传 Word 中提取的评分标准
    reference_text: str | None = None  # 上传 PDF 中提取的原题与标准答案文字
    text: str | None = None  # 由评审器按需填充的文档正文
    images: list[ImagePart] = field(default_factory=list)  # 题面 + 作答，按顺序
    images_sent: int = 0


@dataclass(slots=True)
class ReviewOutcome:
    total_score: float
    comment: str
    model: str = "unknown"
    prompt_version: str = PROMPT_VERSION
    duration_ms: int | None = None
    raw_response: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    images_sent: int = 0


class BaseReviewer(ABC):
    name: str = "base"

    @abstractmethod
    def review(self, context: ReviewContext) -> ReviewOutcome:
        """评审一份提交，返回总分与评语。"""


class MockReviewer(BaseReviewer):
    """占位评审器：按（学号 + 文件名 + 文件内容）稳定出分，方便联调与回归。"""

    name = "mock"

    _COMMENTS = {
        "high": "完成质量较高：内容完整、论述清晰，关键步骤与结论都对得上。",
        "mid": "基本达到要求：主体内容齐全，部分细节和论证可以再展开一些。",
        "low": "完成度偏低：关键步骤说明不足，结论缺少依据，建议对照要求补充。",
    }

    def review(self, context: ReviewContext) -> ReviewOutcome:
        digest = hashlib.sha256()
        digest.update(context.student_no.encode("utf-8"))
        digest.update(context.filename.encode("utf-8"))
        digest.update(context.file_bytes[:4096])
        seed = int(digest.hexdigest()[:12], 16)

        total = context.total_score or 100.0
        bucket = seed % 100
        ratio = 0.62 + bucket / 100 * 0.36  # 62% ~ 98%
        score = round(total * ratio, 1)
        if ratio >= 0.88:
            comment = self._COMMENTS["high"]
        elif ratio >= 0.74:
            comment = self._COMMENTS["mid"]
        else:
            comment = self._COMMENTS["low"]

        return ReviewOutcome(
            total_score=score,
            comment=f"[占位评审] {comment}（满分 {total:g}，本次 {score:g} 分）",
            model="mock-reviewer",
            prompt_version=PROMPT_VERSION,
            images_sent=len(context.images),
        )


class LLMReviewer(BaseReviewer):
    """真实大模型评审：把「题面图 + 学生作答图 + 评分细则」一起发给多模态模型。"""

    name = "llm"

    def review(self, context: ReviewContext) -> ReviewOutcome:
        if context.kind == "lab_report":
            system_prompt = (
                "你是一名严格但公正的大学实验课助教，负责批改学生提交的 PDF 实验报告。"
                "学生报告页面按 PDF 页序提供。实验原理、公式、数据表、图表和计算过程"
                "都要结合页面视觉核对；PDF 文本层中的乱码或缺失不能当作学生未写。"
                "只依据报告页面和老师给的评分细则给分，不要臆测学生没写的内容。"
                "字迹潦草本身不扣分（除非评分细则明确有书写分）。"
            )
        else:
            system_prompt = (
                "你是一名严格但公正的大学课程助教，负责批改学生的作业。"
                "你会拿到题目图片和学生的手写作答照片，请按老师给的评分细则打分。"
                "只依据图片里实际写的内容给分，不要臆测学生没写的东西。"
                "字迹潦草本身不扣分（除非评分细则里明确有书写分）。"
            )
        user_prompt = build_review_prompt(context)
        messages = build_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=context.images,
        )
        limit = context.total_score or 100.0
        expected_items = rubric_breakdown_items(context)
        result = None
        payload: dict[str, Any] | None = None
        breakdown: list[dict[str, Any]] | None = None
        score: float | None = None
        validation_error: LLMError | None = None
        for attempt in range(2):
            result = chat(messages, images_sent=len(context.images))
            try:
                payload = parse_json_object(result.text)
                breakdown, score = _validated_breakdown_total(
                    payload.get("breakdown"), limit, expected_items=expected_items
                )
                break
            except LLMError as exc:
                validation_error = exc
                if attempt == 0:
                    messages.extend(
                        [
                            {"role": "assistant", "content": result.text},
                            {
                                "role": "user",
                                "content": (
                                    f"上一次输出未通过后端校验：{exc}。"
                                    "请重新检查报告，只返回合法的 JSON。"
                                    "breakdown 必须使用指定的大项，且每个大项的 score "
                                    "是该大项的最终得分，不是小项满分之和。"
                                ),
                            },
                        ]
                    )
        if result is None or payload is None or breakdown is None or score is None:
            raise validation_error or LLMError("模型没有返回可用的评分结果")

        comment = str(payload.get("comment") or "").strip()
        raw_total = _optional_decimal(payload.get("score"))

        lines = [f"{item['item']}：{item['score']:g}" for item in breakdown]
        comment = "【分项】" + "；".join(lines) + "\n" + comment
        if raw_total is None or raw_total != Decimal(str(score)):
            reported = "未返回有效总分" if raw_total is None else f"{raw_total:g}"
            comment += (
                f"\n（模型返回总分 {reported}，分项合计 {score:g}；"
                "最终总分以后端分项合计为准）"
            )

        return ReviewOutcome(
            total_score=score,
            comment=comment or "（模型没有给出评语）",
            model=result.model,
            prompt_version=PROMPT_VERSION,
            duration_ms=result.duration_ms,
            raw_response=result.text,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            images_sent=result.images_sent,
        )


def _optional_decimal(value: Any) -> Decimal | None:
    """解析模型总分；它只用于校对，不作为最终成绩。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _validated_breakdown_total(
    breakdown: Any,
    limit: float,
    *,
    expected_items: list[str] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """校验模型返回的每个分项，并由后端计算唯一可信的总分。"""
    if not isinstance(breakdown, list) or not breakdown:
        raise LLMError("模型没有返回分项得分，无法由后端核算总分")

    max_score = Decimal(str(limit))
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    total = Decimal("0")
    for index, item in enumerate(breakdown, start=1):
        if not isinstance(item, dict):
            raise LLMError(f"第 {index} 个评分分项格式不正确")
        name = str(item.get("item") or "").strip()
        if not name:
            raise LLMError(f"第 {index} 个评分分项缺少名称")
        key = name.casefold()
        if key in names:
            raise LLMError(f"评分分项重复：{name}")
        names.add(key)

        value = item.get("score")
        score = _optional_decimal(value)
        if score is None:
            raise LLMError(f"评分分项「{name}」的得分不是有效数字：{value!r}")
        if score < 0:
            raise LLMError(f"评分分项「{name}」不能为负数：{score:g}")
        if score > max_score:
            raise LLMError(f"评分分项「{name}」超过满分：{score:g} > {max_score:g}")

        total += score
        normalized.append({"item": name, "score": float(score)})

    if expected_items:
        actual_names = [_normalize_item_name(item["item"]) for item in normalized]
        expected_names = [_normalize_item_name(item) for item in expected_items]
        if actual_names != expected_names:
            expected_text = "；".join(expected_items)
            actual_text = "；".join(item["item"] for item in normalized)
            raise LLMError(
                "分项名称或顺序不符合评分细则。"
                f"要求：{expected_text}。实际：{actual_text}"
            )

    if total > max_score:
        raise LLMError(f"分项合计 {total:g} 超过满分 {max_score:g}")

    final_score = float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return normalized, final_score


def _normalize_item_name(value: str) -> str:
    name = value.strip()
    name = re.sub(r"^[一二三四五六七八九十]+、\s*", "", name)
    name = re.sub(r"^\d+[.、]\s*", "", name)
    name = re.sub(r"[（(][^（）()]*[）)]\s*$", "", name)
    return re.sub(r"\s+", "", name).casefold()


_TOP_LEVEL_HEADING = re.compile(
    r"^\s*([一二三四五六七八九十]+)、\s*(.+?)\s*$", re.MULTILINE
)


def rubric_breakdown_items(context: ReviewContext) -> list[str]:
    """从评分标准中提取固定的大项，作为 breakdown 的唯一汇总层级。

    大项内部的小点用于解释扣分，不参与 breakdown，避免同一份细则有时按
    3 个大项返回、有时按十几个小点返回。
    """
    sources = [
        context.criteria_document_text,
        context.criteria,
        context.rubric_description,
    ]
    for source in sources:
        if not source:
            continue
        block = source
        if "【评分标准】" in block:
            block = block.split("【评分标准】", 1)[1]
        if "【输出格式要求】" in block:
            block = block.split("【输出格式要求】", 1)[0]
        items = []
        for match in _TOP_LEVEL_HEADING.finditer(block):
            title = re.sub(r"[（(][^（）()]*[）)]\s*$", "", match.group(2)).strip()
            if title:
                items.append(title)
        if len(items) >= 2:
            return items
    return []


_REVIEWERS: dict[str, type[BaseReviewer]] = {
    MockReviewer.name: MockReviewer,
    LLMReviewer.name: LLMReviewer,
}


def get_reviewer(name: str | None = None) -> BaseReviewer:
    key = (name or settings.ai_reviewer or "mock").lower()
    if key not in _REVIEWERS:
        raise ValueError(f"未知评审器：{key}，可选 {sorted(_REVIEWERS)}")
    return _REVIEWERS[key]()


def register_reviewer(name: str, reviewer_cls: type[BaseReviewer]) -> None:
    """扩展点：自定义评审器注册进来即可用。"""
    _REVIEWERS[name] = reviewer_cls


def build_review_prompt(context: ReviewContext) -> str:
    """多模态评审用的 prompt：题面在图片里，这里给文字部分。"""
    kind_label = "实验报告" if context.kind == "lab_report" else "作业"
    references = [img for img in context.images if img.role == "reference"]
    questions = [img for img in context.images if img.role == "question"]
    answers = [img for img in context.images if img.role not in {"question", "reference"}]

    lines: list[str] = [
        f"请批改《{context.rubric_name}》这份{kind_label}。",
        "",
        "【评分细则】",
        context.criteria or "（老师没有填评分细则，请按题目要求的一般标准给分）",
        "",
        f"满分：{context.total_score:g} 分。",
    ]
    expected_items = rubric_breakdown_items(context)
    if context.criteria_document_text:
        lines += ["", "【Word 评分标准】", context.criteria_document_text]
    if context.reference_text:
        lines += ["", "【PDF 原题与标准答案文字】", context.reference_text]
    if context.rubric_description:
        lines += ["", "【任务说明】", context.rubric_description]
    if context.extra_prompt:
        lines += ["", "【额外要求】", context.extra_prompt]

    lines += ["", "【图片说明】"]
    if references:
        lines.append(f"最前面 {len(references)} 张是老师上传的 PDF 原题与标准答案页面。")
    if questions:
        lines.append(f"其中 {len(questions)} 张是题目图片（题面）。")
    if answers:
        answer_label = (
            "这位学生提交的实验报告页面，已按 PDF 页序排列"
            if context.kind == "lab_report"
            else "这位学生的手写作答"
        )
        lines.append(f"最后 {len(answers)} 张是{answer_label}。")
    if not context.images:
        lines.append("这次没有可用的图片。")

    lines += [
        "",
        f"学生：{context.student_name}（{context.student_no}）",
        "",
        "请以 Word 评分标准为主要评分依据，同时对照 PDF 中的原题与标准答案逐题检查学生解答。"
        "不能因为学生答案的文字表达与标准答案不同就直接扣分，应先判断物理意义、公式和计算结果是否等价。"
        "然后严格按下面的 JSON 返回，不要输出别的内容：",
        '{"score": 分数, "breakdown": [{"item": "细则条目名", "score": 该项得分}], "comment": "评语"}',
        "",
        "要求：",
        f"- score 是 0 到 {context.total_score:g} 之间的数字，可以带一位小数；",
        "- breakdown 要覆盖评分细则里的每一条，分数加起来等于 score；",
        "- comment 用中文写 2~4 句，说清楚「哪几点做到了、哪几点扣分、扣在哪一步」，要引用学生解答里的具体内容，不要写空话；",
        "- 学生没写、写错、或者只写结论没有过程，都要在评语里指出来。",
    ]
    if expected_items:
        lines += [
            "",
            "【分项汇总约束】",
            "breakdown 必须且只能返回以下大项，名称和顺序必须完全一致：",
            *[f"{index}. {item}" for index, item in enumerate(expected_items, start=1)],
            "大项内部的小点、扣分依据和判定过程写进 comment，严禁把小点混入 breakdown。",
            "每个大项的 score 是该大项扣除错误后的最终得分，不是满分、不是扣分，"
            "也不是大项内部各小项得分之和；所有大项最终得分之和就是总分。",
        ]
    return "\n".join(lines)


def build_prompt(context: ReviewContext) -> str:
    """纯文本评审用的 prompt（没有图片时的兜底）。"""
    kind_label = "实验报告" if context.kind == "lab_report" else "作业"
    lines: list[str] = [
        f"你是一名严格但公正的课程助教，正在批改学生的{kind_label}《{context.rubric_name}》。",
    ]
    if context.rubric_description:
        lines.append(f"任务说明 / 题目要求：\n{context.rubric_description}")
    if context.criteria:
        lines.append(f"评分细则：\n{context.criteria}")
    if context.extra_prompt:
        lines.append(f"补充要求：{context.extra_prompt}")
    lines.append(f"满分：{context.total_score:g} 分。请给出一个总分和一段评语，不要分维度。")
    lines.append(f"学生：{context.student_name}（{context.student_no}）")
    lines.append(f"提交文件：{context.filename}")
    if context.text:
        lines.append(f"文档正文：\n{context.text}")
    else:
        lines.append("（未能抽取正文，请仅根据文件信息给出谨慎的评语。）")
    lines.append('请严格按 JSON 返回：{"score": 数字, "comment": "评语"}')
    return "\n".join(lines)


def prompt_payload(context: ReviewContext) -> dict[str, Any]:
    """给真实模型调用的结构化入参（便于落日志 / 单测）。"""
    return {
        "prompt": build_prompt(context),
        "prompt_version": PROMPT_VERSION,
        "text": context.text,
        "student_no": context.student_no,
        "filename": context.filename,
        "total_score": context.total_score,
    }
