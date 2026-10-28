"""真实评审器必须由后端按分项汇总总分。"""

from __future__ import annotations

import json

import pytest

from app.services import reviewer
from app.services.llm_client import LLMError, LLMResult
from app.services.reviewer import LLMReviewer, ReviewContext


def context() -> ReviewContext:
    return ReviewContext(
        rubric_name="实验报告",
        kind="lab_report",
        rubric_description="完成实验并提交报告",
        criteria="完整性 60 分；数据处理 30 分；简答题 10 分",
        extra_prompt=None,
        total_score=100,
        student_no="20260001",
        student_name="测试学生",
        filename="report.pdf",
        content_type="application/pdf",
        file_bytes=b"%PDF",
    )


def patch_llm(monkeypatch, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr(
        reviewer,
        "chat",
        lambda *args, **kwargs: LLMResult(
            text=text,
            model="test-vision",
            prompt_tokens=10,
            completion_tokens=5,
            images_sent=1,
        ),
    )


def test_total_comes_from_breakdown_when_model_total_differs(monkeypatch):
    patch_llm(
        monkeypatch,
        {
            "score": 100,
            "breakdown": [
                {"item": "完整性", "score": 60},
                {"item": "数据处理", "score": 20},
                {"item": "简答题", "score": 10},
            ],
            "comment": "测试",
        },
    )

    outcome = LLMReviewer().review(context())

    assert outcome.total_score == 90
    assert "最终总分以后端分项合计为准" in outcome.comment
    assert "60；数据处理：20；简答题：10" in outcome.comment


def test_backend_rejects_breakdown_over_total(monkeypatch):
    patch_llm(
        monkeypatch,
        {
            "score": 100,
            "breakdown": [
                {"item": "完整性", "score": 70},
                {"item": "数据处理", "score": 40},
            ],
            "comment": "测试",
        },
    )

    with pytest.raises(LLMError, match="超过满分"):
        LLMReviewer().review(context())


def test_backend_uses_breakdown_when_model_total_is_missing(monkeypatch):
    patch_llm(
        monkeypatch,
        {
            "breakdown": [
                {"item": "完整性", "score": 55.5},
                {"item": "数据处理", "score": 25},
                {"item": "简答题", "score": 8},
            ],
            "comment": "测试",
        },
    )

    outcome = LLMReviewer().review(context())

    assert outcome.total_score == 88.5
    assert "未返回有效总分" in outcome.comment


def test_breakdown_must_use_fixed_top_level_items(monkeypatch):
    detailed_context = context()
    detailed_context.rubric_description = """
【评分标准】
一、实验报告完整性
1. 实验目的
2. 实验仪器
二、数据处理
1. 拟合图
2. 相对误差
三、简答题
1. 逐题回答
【输出格式要求】
一、完整性检查
"""
    patch_llm(
        monkeypatch,
        {
            "score": 90,
            "breakdown": [
                {"item": "一、实验报告完整性-实验目的", "score": 30},
                {"item": "一、实验报告完整性-实验仪器", "score": 30},
                {"item": "二、数据处理", "score": 20},
                {"item": "三、简答题", "score": 10},
            ],
            "comment": "小点混入了汇总",
        },
    )

    with pytest.raises(LLMError, match="分项名称或顺序"):
        LLMReviewer().review(detailed_context)


def test_breakdown_accepts_fixed_top_level_items(monkeypatch):
    detailed_context = context()
    detailed_context.rubric_description = """
【评分标准】
一、实验报告完整性
二、数据处理
三、简答题
【输出格式要求】
一、完整性检查
"""
    patch_llm(
        monkeypatch,
        {
            "score": 100,
            "breakdown": [
                {"item": "一、实验报告完整性", "score": 60},
                {"item": "二、数据处理", "score": 30},
                {"item": "三、简答题", "score": 10},
            ],
            "comment": "完整检查",
        },
    )

    outcome = LLMReviewer().review(detailed_context)

    assert outcome.total_score == 100
    assert outcome.comment.startswith("【分项】")


def test_invalid_first_format_gets_one_corrective_retry(monkeypatch):
    detailed_context = context()
    detailed_context.rubric_description = """
【评分标准】
一、实验报告完整性
二、数据处理
三、简答题
"""
    outputs = iter(
        [
            LLMResult(
                text=json.dumps(
                    {
                        "score": 100,
                        "breakdown": [
                            {"item": "一、实验报告完整性-实验目的", "score": 10},
                            {"item": "一、实验报告完整性-实验仪器", "score": 10},
                            {"item": "二、数据处理", "score": 70},
                            {"item": "三、简答题", "score": 10},
                        ],
                        "comment": "错误层级",
                    },
                    ensure_ascii=False,
                ),
                model="test-vision",
            ),
            LLMResult(
                text=json.dumps(
                    {
                        "score": 100,
                        "breakdown": [
                            {"item": "一、实验报告完整性", "score": 60},
                            {"item": "二、数据处理", "score": 30},
                            {"item": "三、简答题", "score": 10},
                        ],
                        "comment": "重试后正确",
                    },
                    ensure_ascii=False,
                ),
                model="test-vision",
            ),
        ]
    )
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return next(outputs)

    monkeypatch.setattr(reviewer, "chat", fake_chat)

    outcome = LLMReviewer().review(detailed_context)

    assert outcome.total_score == 100
    assert "重试后正确" in outcome.comment
    assert len(calls) == 2
    assert "未通过后端校验" in calls[1][-1]["content"]
