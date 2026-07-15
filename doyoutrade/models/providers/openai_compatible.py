"""OpenAI-compatible chat provider (official openai Python SDK)."""

from __future__ import annotations

import json
from typing import Any

from doyoutrade.models.base import (
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    log_model_response_debug,
)
from doyoutrade.models.providers._common import (
    PseudoAIMessage,
    _first_chat_completion_message,
    build_openai_messages,
    extract_text,
    image_redacted_block,
    json_safe,
    recordable_request_params,
    recordable_sdk_response,
    redact_image_blocks,
)
from doyoutrade.models.reasoning_tags import ReasoningTagStreamPartitioner, strip_reasoning_tags


def _tool_calls_to_openai_chat_format(tool_calls: list[Any]) -> list[dict[str, Any]]:
    """Build Chat Completions ``tool_calls`` for an assistant message (SDK / PseudoToolCall / dict)."""
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if (
            isinstance(tc, dict)
            and tc.get("type") == "function"
            and isinstance(tc.get("function"), dict)
        ):
            fn = tc["function"]
            arguments = fn.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            out.append(
                {
                    "id": str(tc.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name", "")),
                        "arguments": arguments,
                    },
                }
            )
            continue
        tc_id = getattr(tc, "id", None)
        if tc_id is None and isinstance(tc, dict):
            tc_id = tc.get("id")
        name = getattr(tc, "name", None)
        if name is None and isinstance(tc, dict):
            name = tc.get("name")
        args = getattr(tc, "args", None)
        if args is None and isinstance(tc, dict):
            args = tc.get("args")
        if not isinstance(name, str):
            name = str(name or "")
        if isinstance(args, str):
            arg_str = args
        elif args is None:
            arg_str = "{}"
        else:
            arg_str = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "id": str(tc_id) if tc_id is not None else "",
                "type": "function",
                "function": {"name": name, "arguments": arg_str},
            }
        )
    return out


class OpenAICompatibleAdapter(ModelAdapter):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        max_tokens: int | None,
        timeout_seconds: float,
        tool_choice: str | dict[str, Any] | None = None,
    ):
        try:
            from openai import OpenAI, AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("openai is not installed") from exc

        self.sync_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )
        self.async_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tool_choice = tool_choice

    def generate(self, request: ModelRequest) -> ModelResponse:
        messages = build_openai_messages(
            request.system_prompt,
            request.user_prompt,
            image_parts=request.image_parts,
        )
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.max_tokens:
            params["max_tokens"] = self.max_tokens
        if request.tools is not None:
            params["tools"] = request.tools
            if self.tool_choice is not None:
                params["tool_choice"] = self.tool_choice

        response = self.sync_client.chat.completions.create(**params)
        chat_message = _first_chat_completion_message(response)
        raw = PseudoAIMessage.from_openai(chat_message)
        # Non-streaming call: no delta callbacks to route reasoning through, so
        # inline <think> markup (see agent_turn's reasoning_partitioner) is
        # simply stripped out of the visible text rather than surfaced verbatim.
        visible_text, _thinking_text = strip_reasoning_tags(extract_text(raw.content))
        out = ModelResponse(
            text=visible_text,
            raw=raw,
            # Image base64 must never land in model_invocations — redact
            # vision blocks down to `<image: N bytes, mime>` placeholders.
            invocation_request_payload=redact_image_blocks(
                recordable_request_params(params)
            ),
            invocation_response_payload=recordable_sdk_response(response),
        )
        log_model_response_debug(out, adapter="openai_compatible")
        return out

    def chat_completions_create_kwargs(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Kwargs passed to ``chat.completions.create`` (OpenAI Chat Completions wire body)."""
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_dicts(messages),
            "temperature": self.temperature,
        }
        if self.max_tokens is not None and self.max_tokens > 0:
            create_kwargs["max_tokens"] = self.max_tokens
        if tools is not None:
            create_kwargs["tools"] = tools
            if self.tool_choice is not None:
                create_kwargs["tool_choice"] = self.tool_choice
        return create_kwargs

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        create_kwargs = self.chat_completions_create_kwargs(messages, tools)
        response = await self.async_client.chat.completions.create(**create_kwargs)
        chat_message = _first_chat_completion_message(response)
        raw = PseudoAIMessage.from_openai(chat_message)
        visible_text, _thinking_text = strip_reasoning_tags(extract_text(raw.content))
        out = ModelResponse(
            text=visible_text,
            raw=raw,
            invocation_request_payload=recordable_request_params(create_kwargs),
            invocation_response_payload=recordable_sdk_response(response),
        )
        log_model_response_debug(out, adapter="openai_compatible")
        return out

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_text_delta=None,
        on_thinking_delta=None,
    ):
        from types import SimpleNamespace

        from doyoutrade.agent_runtime import AgentTurnResponse, agent_turn_response_from_model_response

        if on_text_delta is None and on_thinking_delta is None:
            return agent_turn_response_from_model_response(
                await self.chat_ainvoke(messages, tools=tools)
            )

        create_kwargs = self.chat_completions_create_kwargs(messages, tools)
        create_kwargs["stream"] = True
        stream = await self.async_client.chat.completions.create(**create_kwargs)
        text_parts: list[str] = []
        chunk_payloads: list[dict[str, Any]] = []
        usage_payload: dict[str, Any] | None = None
        tool_call_parts: dict[int, dict[str, Any]] = {}
        # Some OpenAI-compatible providers (e.g. MiniMax) inline chain-of-thought
        # into plain ``content`` as ``<think>...</think>`` instead of a dedicated
        # ``reasoning_content`` delta field. Split it back out here so it never
        # leaks into the visible message text / content_blocks.
        reasoning_partitioner = ReasoningTagStreamPartitioner()

        async def _dispatch(kind: str, text: str) -> None:
            if not text:
                return
            if kind == "text":
                text_parts.append(text)
                if on_text_delta is not None:
                    maybe_awaitable = on_text_delta(text)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
            elif on_thinking_delta is not None:
                maybe_awaitable = on_thinking_delta(text)
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable

        async for chunk in stream:
            chunk_payloads.append(recordable_sdk_response(chunk))
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = recordable_sdk_response(usage)
                if not isinstance(usage_payload, dict):
                    usage_payload = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "total_tokens": getattr(usage, "total_tokens", None),
                    }
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                for kind, text in reasoning_partitioner.push(content):
                    await _dispatch(kind, text)
            # Reasoning / thinking delta (OpenAI-compatible convention, e.g. DeepSeek's
            # ``reasoning_content`` field). Forward to ``on_thinking_delta`` when present.
            if on_thinking_delta is not None:
                thinking = getattr(delta, "reasoning_content", None)
                if not isinstance(thinking, str) or not thinking:
                    thinking = getattr(delta, "thinking", None)
                if isinstance(thinking, str) and thinking:
                    maybe_awaitable = on_thinking_delta(thinking)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
            for raw_tc in getattr(delta, "tool_calls", None) or []:
                idx = int(getattr(raw_tc, "index", 0) or 0)
                state = tool_call_parts.setdefault(
                    idx,
                    {"id": "", "name": "", "arguments": ""},
                )
                tc_id = getattr(raw_tc, "id", None)
                if tc_id:
                    state["id"] = str(tc_id)
                fn = getattr(raw_tc, "function", None)
                if fn is not None:
                    name = getattr(fn, "name", None)
                    if name:
                        state["name"] += str(name)
                    args = getattr(fn, "arguments", None)
                    if args:
                        state["arguments"] += str(args)

        for kind, text_piece in reasoning_partitioner.flush():
            await _dispatch(kind, text_piece)

        text = "".join(text_parts)
        tool_calls = [
            SimpleNamespace(
                id=state["id"],
                function=SimpleNamespace(
                    name=state["name"],
                    arguments=state["arguments"] or "{}",
                ),
            )
            for _, state in sorted(tool_call_parts.items())
            if state["name"]
        ]
        raw = PseudoAIMessage.from_openai(
            SimpleNamespace(content=text, tool_calls=tool_calls or None)
        )
        if isinstance(usage_payload, dict):
            raw.usage_metadata = {
                "input_tokens": usage_payload.get("prompt_tokens")
                or usage_payload.get("input_tokens"),
                "output_tokens": usage_payload.get("completion_tokens")
                or usage_payload.get("output_tokens"),
                "total_tokens": usage_payload.get("total_tokens"),
            }
        response_payload = {
            "stream": True,
            "chunks": chunk_payloads,
            "content": text,
        }
        if isinstance(usage_payload, dict):
            response_payload["usage"] = usage_payload
        turn = agent_turn_response_from_model_response(
            ModelResponse(
                text=text,
                raw=raw,
                invocation_request_payload=recordable_request_params(create_kwargs),
                invocation_response_payload=response_payload,
            )
        )
        return turn

    def _messages_to_dicts(self, messages: list[Any]) -> list[dict[str, Any]]:
        """Convert LangChain-style messages to OpenAI message dicts."""
        result: list[dict[str, Any]] = []
        pending_skill_chunks: list[str] = []

        def flush_skill_chunks() -> None:
            if pending_skill_chunks:
                result.append(
                    {
                        "role": "user",
                        "content": "\n\n---\n\n".join(pending_skill_chunks),
                    }
                )
                pending_skill_chunks.clear()

        for msg in messages:
            msg_type = getattr(msg, "type", "user")
            if msg_type == "system":
                role = "system"
            elif msg_type == "human":
                role = "user"
            elif msg_type == "tool":
                role = "tool"
            else:
                role = "assistant"

            if role == "tool":
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        b if isinstance(b, str) else b.get("text", str(b))
                        for b in content
                    )
                entry: dict[str, Any] = {"role": role, "content": str(content)}
                tc_id = getattr(msg, "tool_call_id", None)
                if tc_id:
                    entry["tool_call_id"] = tc_id
                result.append(entry)
                companion = getattr(msg, "companion_user_text", None)
                if isinstance(companion, str) and companion.strip():
                    pending_skill_chunks.append(companion.strip())
                continue

            flush_skill_chunks()

            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = "\n".join(
                    b if isinstance(b, str) else b.get("text", str(b))
                    for b in content
                )
            entry = {"role": role, "content": str(content)}
            if role == "assistant":
                tcalls = getattr(msg, "tool_calls", None)
                if tcalls:
                    entry["tool_calls"] = _tool_calls_to_openai_chat_format(list(tcalls))
            result.append(entry)

        flush_skill_chunks()
        return result


def serialized_model_invocation_request_body(
    adapter: OpenAICompatibleAdapter,
    request: ModelRequest,
) -> dict[str, Any]:
    user_content: Any = request.user_prompt
    if request.image_parts:
        # Recorded pre-call body never carries base64 — placeholder blocks only.
        user_content = [{"type": "text", "text": request.user_prompt}] + [
            image_redacted_block(len(part.data), part.mime_type)
            for part in request.image_parts
        ]
    body: dict[str, Any] = {
        "model": adapter.model,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    if adapter.temperature is not None:
        body["temperature"] = adapter.temperature
    if adapter.max_tokens:
        body["max_tokens"] = adapter.max_tokens
    if request.tools is not None:
        body["tools"] = json_safe(request.tools)
        if adapter.tool_choice is not None:
            body["tool_choice"] = (
                json_safe(adapter.tool_choice)
                if isinstance(adapter.tool_choice, dict)
                else adapter.tool_choice
            )
    return body
