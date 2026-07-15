"""Anthropic Messages API provider (official anthropic Python SDK)."""

from __future__ import annotations

from typing import Any

import httpx

from doyoutrade.models.base import (
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    log_model_response_debug,
)
from doyoutrade.models.providers._common import (
    PseudoAIMessage,
    apply_wire_usage_to_pseudo_message,
    build_anthropic_messages,
    chat_ainvoke_anthropic,
    extract_text,
    image_redacted_block,
    json_safe,
    openai_function_tools_to_anthropic,
    recordable_anthropic_sdk_response,
    recordable_request_params,
    redact_image_blocks,
)


# Streaming reads must tolerate long inter-chunk SSE gaps. A scalar route timeout
# (default 30s) was forwarded verbatim to httpx, which makes read==connect==write==pool;
# the READ timeout is the max gap *between* streamed chunks, so a slow first token or a
# long mid-stream pause raised httpx.ReadTimeout and killed the whole agent turn. We
# decouple read from the scalar: connect stays tight, read gets a generous floor.
_STREAM_READ_TIMEOUT_FLOOR_SECONDS = 300.0
_CONNECT_TIMEOUT_CEILING_SECONDS = 10.0


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        model: str,
        api_key: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        base_url: str | None = None,
        thinking: dict[str, Any] | None = None,
        cache_control: dict[str, Any] | None = None,
    ):
        try:
            from anthropic import Anthropic, AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError("anthropic is not installed") from exc

        timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(timeout_seconds, _CONNECT_TIMEOUT_CEILING_SECONDS),
            read=max(timeout_seconds, _STREAM_READ_TIMEOUT_FLOOR_SECONDS),
        )
        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.async_client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.cache_control = cache_control

    def generate(self, request: ModelRequest) -> ModelResponse:
        messages = build_anthropic_messages(
            request.system_prompt,
            request.user_prompt,
            image_parts=request.image_parts,
        )
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.thinking is not None:
            params["thinking"] = self.thinking
        if self.cache_control is not None:
            params["cache_control"] = self.cache_control
        if request.tools is not None:
            params["tools"] = openai_function_tools_to_anthropic(request.tools)

        raw_resp = self.client.messages.with_raw_response.create(**params)
        message = raw_resp.parse()
        wire_json = None
        try:
            body = raw_resp.http_response.json()
            if isinstance(body, dict):
                wire_json = body
        except Exception:
            wire_json = None
        raw = PseudoAIMessage.from_anthropic(message)
        apply_wire_usage_to_pseudo_message(raw, wire_json)
        out = ModelResponse(
            text=extract_text(raw.content),
            raw=raw,
            # Image base64 must never land in model_invocations — redact
            # vision blocks down to `<image: N bytes, mime>` placeholders.
            invocation_request_payload=redact_image_blocks(
                recordable_request_params(params)
            ),
            invocation_response_payload=recordable_anthropic_sdk_response(message, wire_json),
        )
        log_model_response_debug(out, adapter="anthropic")
        return out

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        return await chat_ainvoke_anthropic(
            client=self.async_client,
            messages=messages,
            tools=tools,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            thinking=self.thinking,
            cache_control=self.cache_control,
        )

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_text_delta=None,
        on_thinking_delta=None,
    ):
        from doyoutrade.agent_runtime import agent_turn_response_from_model_response
        from doyoutrade.models.providers._common import anthropic_messages_api_params

        if on_text_delta is None and on_thinking_delta is None:
            return agent_turn_response_from_model_response(
                await self.chat_ainvoke(messages, tools=tools)
            )

        anthropic_tools = openai_function_tools_to_anthropic(tools) if tools else None
        params = anthropic_messages_api_params(
            messages,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            tools=anthropic_tools,
            thinking=self.thinking,
            cache_control=self.cache_control,
        )
        stream_events: list[dict[str, Any]] = []
        async with self.async_client.messages.stream(**params) as stream:
            async for event in stream:
                stream_events.append(recordable_request_params({"event": event}))
                if getattr(event, "type", None) != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                # Text delta (from text content blocks or thinking blocks that produce visible text)
                text = getattr(delta, "text", None)
                if isinstance(text, str) and text:
                    maybe_awaitable = on_text_delta(text)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
                # Thinking delta (extended thinking block)
                thinking = getattr(delta, "thinking", None)
                if isinstance(thinking, str) and thinking:
                    maybe_awaitable = on_thinking_delta(thinking)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
            message = await stream.get_final_message()

        raw = PseudoAIMessage.from_anthropic(message)
        response_payload = recordable_anthropic_sdk_response(message, None)
        if not isinstance(response_payload, dict):
            response_payload = {"message": response_payload}
        response_payload["stream"] = True
        response_payload["events"] = stream_events
        turn = agent_turn_response_from_model_response(
            ModelResponse(
                text=extract_text(raw.content),
                raw=raw,
                invocation_request_payload=recordable_request_params(params),
                invocation_response_payload=response_payload,
            )
        )
        return turn


def serialized_model_invocation_request_body(
    adapter: AnthropicAdapter,
    request: ModelRequest,
) -> dict[str, Any]:
    user_content: Any = request.user_prompt
    if request.image_parts:
        # Recorded pre-call body never carries base64 — placeholder blocks only.
        user_content = [
            image_redacted_block(len(part.data), part.mime_type)
            for part in request.image_parts
        ] + [{"type": "text", "text": request.user_prompt}]
    body: dict[str, Any] = {
        "model": adapter.model,
        "max_tokens": adapter.max_tokens,
        "system": request.system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    if adapter.temperature is not None:
        body["temperature"] = adapter.temperature
    if adapter.thinking is not None:
        body["thinking"] = json_safe(adapter.thinking)
    if adapter.cache_control is not None:
        body["cache_control"] = json_safe(adapter.cache_control)
    if request.tools is not None:
        body["tools"] = json_safe(openai_function_tools_to_anthropic(request.tools))
    return body
