"""大模型调用（OpenAI 兼容的 /chat/completions，DeepSeek 也是这一套）。

只依赖 httpx，不引入各家 SDK：换服务商只需要改 .env 里的 LLM_BASE_URL / LLM_MODEL。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、返回格式不对等）。"""


@dataclass(slots=True)
class ImagePart:
    """要发给模型的图片。"""

    filename: str
    content_type: str
    data: bytes
    role: str = "answer"  # question / answer


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    images_sent: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _data_url(image: ImagePart) -> str:
    payload = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.content_type};base64,{payload}"


def build_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    images: list[ImagePart] | None = None,
) -> list[dict[str, Any]]:
    """拼 OpenAI 风格的多模态消息：文字 + 一串 image_url。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for image in images or []:
        content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def chat(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    images_sent: int = 0,
    json_object: bool = True,
) -> LLMResult:
    """调一次对话补全，带重试。"""
    if not settings.llm_api_key:
        raise LLMError("还没配 LLM_API_KEY，请在 backend/.env 里填上大模型的 key")

    base = settings.llm_base_url.rstrip("/")
    url = f"{base}/chat/completions"
    payload: dict[str, Any] = {
        "model": model or settings.llm_model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "stream": False,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    if settings.llm_disable_thinking:
        # 关掉「思考」：不关的话模型能把几千 token 全花在推理上，还可能
        # finish_reason=length 直接不输出答案（实测最多思考过 5 万字）
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    last: Exception | None = None
    for attempt in range(max(1, settings.llm_retries + 1)):
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=settings.llm_timeout, trust_env=False) as client:
                resp = client.post(url, json=payload, headers=headers)
                if (
                    resp.status_code == 400
                    and "thinking" in payload
                    and "thinking" in resp.text.lower()
                ):
                    # 换个网关可能不认这个字段，摘掉再试一次
                    logger.warning("网关不认 thinking 参数，去掉后重试：%s", resp.text[:120])
                    payload.pop("thinking", None)
                    resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(f"模型返回 {resp.status_code}：{resp.text[:300]}")
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            finish = choice.get("finish_reason")
            thinking = message.get("reasoning_content") or ""
            if not text.strip():
                # deepseek-flash 这类「带思考」的模型会把 token 先花在 reasoning 上，
                # 配额不够时 finish_reason=length、content 直接是空的
                hint = (
                    "模型把 token 都用在思考上了，没来得及输出答案，"
                    f"请把 LLM_MAX_TOKENS 调大（当前 {payload['max_tokens']}）"
                    if finish == "length"
                    else "模型没有返回内容"
                )
                raise LLMError(
                    f"{hint}；finish_reason={finish}，思考了 {len(thinking)} 字，"
                    f"用量 {data.get('usage')}"
                )
            usage = data.get("usage") or {}
            result = LLMResult(
                text=text,
                model=data.get("model") or payload["model"],
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                duration_ms=int((time.perf_counter() - started) * 1000),
                images_sent=images_sent,
                raw=data,
            )
            if settings.llm_log_usage:
                logger.info(
                    "模型 %s 返回：输入 %s token / 输出 %s token / 耗时 %s ms / 图片 %s 张",
                    result.model,
                    result.prompt_tokens,
                    result.completion_tokens,
                    result.duration_ms,
                    images_sent,
                )
            return result
        except (httpx.HTTPError, LLMError, ValueError) as exc:
            last = exc
            if attempt < settings.llm_retries:
                wait = 2**attempt
                logger.warning("模型调用失败（第 %s 次）：%s，%s 秒后重试", attempt + 1, exc, wait)
                time.sleep(wait)
    raise LLMError(f"模型调用失败：{last}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型返回里抠出 JSON（有的模型爱套一层 ```json）。"""
    body = (text or "").strip()
    match = _FENCE.search(body)
    if match:
        body = match.group(1).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"模型没返回 JSON：{text[:200]}") from None
        try:
            data = json.loads(body[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的 JSON 解析不了：{exc}；原文 {text[:200]}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"模型返回的不是 JSON 对象：{text[:200]}")
    return data
