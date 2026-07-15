"""Dispatch serialization for model invocation recording across providers."""

from __future__ import annotations

from typing import Any

from doyoutrade.models.base import ModelAdapter, ModelRequest
from doyoutrade.models.providers._common import (
    anthropic_messages_api_params,
    json_safe,
    openai_function_tools_to_anthropic,
    recordable_request_params,
    serialize_lc_messages,
)
from doyoutrade.models.providers.anthropic import (
    AnthropicAdapter,
    serialized_model_invocation_request_body as anthropic_model_body,
)
from doyoutrade.models.providers.lmstudio import LmStudioAdapter
from doyoutrade.models.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    serialized_model_invocation_request_body as openai_model_body,
)


def serialized_model_invocation_request(adapter: ModelAdapter, request: ModelRequest) -> dict[str, Any]:
    """JSON-serializable body matching the provider chat HTTP API.

    OpenAI-compatible: ``messages`` with ``system`` / ``user`` roles.
    Anthropic Messages API: top-level ``system`` plus ``messages`` with the user turn.
    Unknown adapters: OpenAI-style ``messages`` only (no provider client metadata).
    """
    if isinstance(adapter, OpenAICompatibleAdapter):
        return openai_model_body(adapter, request)
    if isinstance(adapter, AnthropicAdapter):
        return anthropic_model_body(adapter, request)
    out: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ],
        "tools": None,
    }
    if request.tools is not None:
        out["tools"] = json_safe(request.tools)
    return out


def serialized_chat_invocation_request(
    adapter: ModelAdapter,
    messages: list[Any],
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """JSON snapshot of a multi-turn chat request matching the provider wire body.

    OpenAI-compatible: same keys as ``chat.completions.create`` (``role`` is ``user``/``assistant``/…).
    Anthropic: Messages API params (structured ``messages``, optional ``system``).
    LM Studio: ``model`` / ``history`` / ``config`` as passed to the SDK.
    Unknown adapters: LangChain-style ``messages`` (``role`` mirrors message ``type``).
    """
    if isinstance(adapter, OpenAICompatibleAdapter):
        return recordable_request_params(adapter.chat_completions_create_kwargs(messages, tools))
    if isinstance(adapter, LmStudioAdapter):
        return recordable_request_params(adapter.build_invocation_record_dict(messages, tools))
    if isinstance(adapter, AnthropicAdapter):
        anthropic_tools = openai_function_tools_to_anthropic(tools) if tools else None
        params = anthropic_messages_api_params(
            messages,
            model=adapter.model,
            max_tokens=adapter.max_tokens,
            temperature=adapter.temperature,
            tools=anthropic_tools,
            thinking=adapter.thinking,
            cache_control=adapter.cache_control,
        )
        return recordable_request_params(params)

    body: dict[str, Any] = {"messages": serialize_lc_messages(messages)}
    if tools is not None:
        body["tools"] = json_safe(tools)
    return body
