from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

try:
    from langchain_core.messages import HumanMessage
except Exception:  # pragma: no cover - dependency fallback for stripped test envs
    from doyoutrade.test_messages import HumanMessage

from doyoutrade.models.base import ModelRequest

if TYPE_CHECKING:
    from doyoutrade.assistant.service import ModelAdapterFactory


TRUNCATE_LIMIT = 800
TITLE_TIMEOUT_SECONDS = 10.0


async def generate_session_title(
    first_message: str,
    model_route_name: str,
    model_adapter_factory: ModelAdapterFactory,
) -> str | None:
    """
    根据首条用户消息调用模型生成 session 标题。

    失败时返回 None（静默降级）。
    """
    content = first_message[:TRUNCATE_LIMIT]

    prompt = f"""根据用户的原始需求，生成一个简洁的 session 标题（3-7 个词，句式大写，即只有首字母和专有名词大写）。

要求：
- 直接描述用户的核心意图（如：分析某只股票、回测某个策略、调试某个问题）
- 不要包含"帮我"、"请问"等开场白词汇
- 可以包含具体标的、策略名称或问题关键词

用户需求：{content}

返回格式（仅 JSON）：
{{"title": "你的标题"}}"""

    try:
        if model_adapter_factory is None:
            return None
        adapter = await model_adapter_factory(model_route_name)
        response = await asyncio.wait_for(
            _invoke_title_model(adapter, prompt),
            timeout=TITLE_TIMEOUT_SECONDS,
        )
        return _extract_title(response.text)
    except Exception:
        return None


async def _invoke_title_model(adapter: object, prompt: str):
    chat_ainvoke = getattr(adapter, "chat_ainvoke", None)
    if callable(chat_ainvoke):
        return await chat_ainvoke([HumanMessage(content=prompt)], tools=None)
    generate = getattr(adapter, "generate", None)
    if callable(generate):
        return await asyncio.to_thread(generate, ModelRequest(system_prompt="", user_prompt=prompt))
    raise AttributeError("model adapter does not support title generation")


def _extract_title(text: str) -> str | None:
    """从模型返回的文本中解析出 title 字段。"""
    if not text:
        return None
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "title" in data:
            title = str(data["title"]).strip()
            if title:
                return title
    except json.JSONDecodeError:
        pass
    return None
