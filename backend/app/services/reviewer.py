"""AI 评审器接口。

评审只输出「一个总分 + 一段评语」，不分维度。
真实的大模型评审逻辑先不做：这里定义好契约 + 一个 mock 实现，
让上传、入库、导出整条链路现在就能跑通；接真模型时只需要实现
`BaseReviewer.review()` 并在 `_REVIEWERS` 里注册。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..config import settings

PROMPT_VERSION = "v2-single-score"


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


@dataclass(slots=True)
class ReviewOutcome:
    total_score: float
    comment: str
    model: str = "unknown"
    prompt_version: str = PROMPT_VERSION
    duration_ms: int | None = None
    raw_response: str | None = None


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
        )


class LLMReviewer(BaseReviewer):
    """真实大模型评审（待实现）。

    接入步骤：
      1. `services.document.extract_text` 已经把文档正文放进 `context.text`；
      2. 用 `build_prompt(context)`（或 `prompt_payload(context)`）组装 prompt；
      3. 要求模型返回 JSON：
         {"score": 88, "comment": "……"}
      4. 把分数 clamp 到 [0, total_score]，组装成 ReviewOutcome 返回。
    """

    name = "llm"

    def review(self, context: ReviewContext) -> ReviewOutcome:  # noqa: ARG002
        raise NotImplementedError(
            "真实 AI 评审尚未接入：请在 services/reviewer.py 的 LLMReviewer.review 里实现"
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


def build_prompt(context: ReviewContext) -> str:
    """真实接入时可直接复用的 prompt 组装。"""
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
