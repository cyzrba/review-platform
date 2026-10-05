"""AI 评审器接口。

评审只输出「一个总分 + 一段评语」，不分维度。
真实的大模型评审逻辑先不做：这里定义好契约 + 一个 mock 实现，
让上传、入库、导出整条链路现在就能跑通；接真模型时只需要实现
`BaseReviewer.review()` 并在 `_REVIEWERS` 里注册。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .llm_client import LLMError, ImagePart, build_messages, chat, parse_json_object

PROMPT_VERSION = "v3-vision-rubric"


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
        result = chat(messages, images_sent=len(context.images))
        payload = parse_json_object(result.text)

        raw_score = payload.get("score")
        try:
            score = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise LLMError(f"模型给的分数不是数字：{raw_score!r}") from exc
        limit = context.total_score or 100.0
        score = max(0.0, min(score, float(limit)))

        comment = str(payload.get("comment") or "").strip()
        breakdown = payload.get("breakdown")
        if breakdown:
            lines = [f"{item.get('item', '')}：{item.get('score', '')}".strip("：")
                     for item in breakdown if isinstance(item, dict)]
            if lines:
                comment = "【分项】" + "；".join(lines) + "\n" + comment

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
    questions = [img for img in context.images if img.role == "question"]
    answers = [img for img in context.images if img.role != "question"]

    lines: list[str] = [
        f"请批改《{context.rubric_name}》这份{kind_label}。",
        "",
        "【评分细则】",
        context.criteria or "（老师没有填评分细则，请按题目要求的一般标准给分）",
        "",
        f"满分：{context.total_score:g} 分。",
    ]
    if context.rubric_description:
        lines += ["", "【任务说明】", context.rubric_description]
    if context.extra_prompt:
        lines += ["", "【额外要求】", context.extra_prompt]

    lines += ["", "【图片说明】"]
    if questions:
        lines.append(f"前 {len(questions)} 张是题目图片（题面）。")
    if answers:
        lines.append(f"后面 {len(answers)} 张是这位学生的手写作答。")
    if not context.images:
        lines.append("这次没有可用的图片。")

    lines += [
        "",
        f"学生：{context.student_name}（{context.student_no}）",
        "",
        "请对照题面逐题检查学生的解答，按上面的评分细则打分，然后严格按下面的 JSON 返回，不要输出别的内容：",
        '{"score": 分数, "breakdown": [{"item": "细则条目名", "score": 该项得分}], "comment": "评语"}',
        "",
        "要求：",
        f"- score 是 0 到 {context.total_score:g} 之间的数字，可以带一位小数；",
        "- breakdown 要覆盖评分细则里的每一条，分数加起来等于 score；",
        "- comment 用中文写 2~4 句，说清楚「哪几点做到了、哪几点扣分、扣在哪一步」，要引用学生解答里的具体内容，不要写空话；",
        "- 学生没写、写错、或者只写结论没有过程，都要在评语里指出来。",
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
