"""Shared helpers for model providers using official SDKs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from doyoutrade.money.decimal_helpers import json_default_with_decimals
from doyoutrade.models.base import ModelResponse, log_model_response_debug


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value, default=json_default_with_decimals)
        return value
    except (TypeError, ValueError):
        return str(value)


def openai_function_tools_to_anthropic(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert OpenAI Chat Completions ``tools`` format to Anthropic Messages API tool format.

    OpenAI format (what :meth:`OperationHandler.to_openai_schema` produces)::

        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

    Anthropic format (what ``client.messages.create(tools=...)`` expects)::

        {"name": "...", "description": "...", "input_schema": {...}}
    """
    result: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if item.get("type") == "function" else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name", "") or "")
        if not name:
            continue
        desc = fn.get("description", "") or ""
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}} if params is None else {}
        result.append(
            {
                "name": name,
                "description": desc,
                "input_schema": params,
            }
        )
    return result


def recordable_request_params(params: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy *params* into JSON-native structures for ``model_invocations.request_payload``."""
    return json.loads(json.dumps(params, default=json_default_with_decimals))


def recordable_sdk_response(obj: Any) -> dict[str, Any]:
    """Serialize a provider SDK response (e.g. OpenAI ``ChatCompletion``) to a JSON dict."""
    if obj is None:
        return {}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            out = model_dump(mode="json")
        except TypeError:
            out = model_dump()
        if isinstance(out, dict):
            return out
    return json.loads(json.dumps(obj, default=json_default_with_decimals))


def recordable_anthropic_sdk_response(
    message: Any,
    wire_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize an Anthropic ``Message`` for ``model_invocations.response_payload``.

    Some gateways return full ``usage`` (cache read/create counts, etc.) on the wire while the
    parsed SDK model may leave optional :class:`anthropic.types.usage.Usage` fields unset
    (serialized as ``null``). When *wire_json* is the decoded HTTP body, we copy ``usage`` from
    it so persistence matches the API.
    """
    out = recordable_sdk_response(message)
    if not wire_json:
        return out
    wire_usage = wire_json.get("usage")
    if isinstance(wire_usage, dict):
        out = dict(out)
        out["usage"] = json_safe(dict(wire_usage))
    return out


def apply_wire_usage_to_pseudo_message(
    raw: PseudoAIMessage,
    wire_json: dict[str, Any] | None,
) -> None:
    """Overlay HTTP ``usage`` onto ``raw.usage_metadata`` (same data as :func:`recordable_anthropic_sdk_response`)."""
    if not wire_json:
        return
    wire_usage = wire_json.get("usage")
    if not isinstance(wire_usage, dict):
        return
    base = dict(raw.usage_metadata or {})
    raw.usage_metadata = {**base, **json_safe(dict(wire_usage))}


def _first_chat_completion_message(response: Any) -> Any:
    """Return the assistant ``message`` from the first choice of a Chat Completions *response*.

    Some OpenAI-compatible gateways return ``choices=None`` or an empty list. Subscripting
    ``response.choices[0]`` then raises ``TypeError: 'NoneType' object is not subscriptable``;
    this helper fails with a clear :class:`RuntimeError` instead.
    """
    choices = getattr(response, "choices", None)
    if choices is None:
        raise RuntimeError(
            "OpenAI-compatible API returned response with choices=None; cannot read assistant message",
        )
    if len(choices) == 0:
        raise RuntimeError(
            "OpenAI-compatible API returned empty choices; cannot read assistant message",
        )
    first = choices[0]
    msg = getattr(first, "message", None)
    if msg is None:
        raise RuntimeError(
            "OpenAI-compatible API returned a choice with no message field",
        )
    return msg


# ---------------------------------------------------------------------------
# Pseudo-AIMessage — LangChain AIMessage-compatible interface over SDK types
# ---------------------------------------------------------------------------


@dataclass
class PseudoToolCall:
    """Duck-types LangChain's ToolCall: name, args (JSON str), id."""
    name: str
    args: str  # JSON string
    id: str | None = None

    @classmethod
    def from_anthropic(cls, block: Any) -> "PseudoToolCall":
        """block is an anthropic.ToolUseBlock."""
        import json as _json
        args = block.input if isinstance(block.input, str) else _json.dumps(block.input)
        return cls(name=block.name, args=args, id=block.id)

    @classmethod
    def from_openai(cls, tc: Any) -> "PseudoToolCall":
        """tc is openai.types.chat.ChatCompletionMessageToolCall."""
        import json as _json
        args = tc.function.arguments if isinstance(tc.function.arguments, str) else _json.dumps(tc.function.arguments)
        return cls(name=tc.function.name, args=args, id=tc.id)


@dataclass
class PseudoAIMessage:
    """LangChain AIMessage-compatible wrapper around official SDK message objects.

    Exposes:
    - content: str | list — text or SDK content blocks
    - tool_calls: list[PseudoToolCall] | None
    - usage_metadata: dict | None — {"input_tokens": int, "output_tokens": int, "total_tokens": int}
    - response_metadata: dict | None — arbitrary provider-specific fields
    - type: str — LangChain-style role marker (``ai``) for message routing
    """

    content: str | list[Any]
    tool_calls: list[PseudoToolCall] | None = None
    usage_metadata: dict[str, Any] | None = None
    response_metadata: dict[str, Any] | None = None
    type: str = "ai"

    # LangChain AIMessage compatibility: allow message to be iterated as content blocks
    def __getitem__(self, key: str) -> Any:
        if key == "content":
            return self.content
        raise KeyError(key)

    @staticmethod
    def _normalize_content(content: Any) -> str | list[Any]:
        """Normalize SDK content to str | list[dict] for extract_text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return content
        return str(content)

    @classmethod
    def from_anthropic(cls, message: Any) -> "PseudoAIMessage":
        """Build from anthropic.Message."""
        tool_calls: list[PseudoToolCall] | None = None
        if hasattr(message, "content") and isinstance(message.content, list):
            tool_calls = [
                cls._block_to_tool_call(b)
                for b in message.content
                if b.type == "tool_use"
            ]
            tool_calls = tool_calls if tool_calls else None

        usage = getattr(message, "usage", None)
        usage_meta: dict[str, Any] | None = None
        if usage is not None:
            model_dump = getattr(usage, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump(mode="json")
                except TypeError:
                    dumped = model_dump()
                usage_meta = dumped if isinstance(dumped, dict) else None
            if usage_meta is None:
                usage_meta = {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }

        response_meta: dict[str, Any] = {}
        stats = getattr(message, "stats", None)
        if stats is not None:
            response_meta["time_to_first_token_ms"] = getattr(stats, "time_to_first_token_ms", None)
            response_meta["stop_reason"] = getattr(message, "stop_reason", None)

        # Flatten content blocks to text
        raw_content = message.content
        if isinstance(raw_content, list):
            texts = []
            for block in raw_content:
                if block.type == "text":
                    texts.append(block.text)
            flat = "\n".join(texts) if texts else ""
            content: str | list[Any] = flat
        else:
            content = str(raw_content) if raw_content else ""

        return cls(
            content=content,
            tool_calls=tool_calls,
            usage_metadata=usage_meta,
            response_metadata=response_meta or None,
        )

    @staticmethod
    def _block_to_tool_call(block: Any) -> PseudoToolCall:
        """Convert an anthropic content block to PseudoToolCall."""
        if block.type == "tool_use":
            return PseudoToolCall.from_anthropic(block)
        raise ValueError(f"Cannot convert block type {block.type}")

    @classmethod
    def from_openai(cls, message: Any) -> "PseudoAIMessage":
        """Build from openai.ChatCompletionMessage."""
        tool_calls: list[PseudoToolCall] | None = None
        raw_tc = getattr(message, "tool_calls", None)
        if raw_tc:
            tool_calls = [PseudoToolCall.from_openai(tc) for tc in raw_tc]

        # Chat Completions usually return str; some OpenAI-compatible gateways return
        # list-shaped parts. Using str(list) breaks JSON fallbacks in agent signal path.
        raw_content = message.content
        if raw_content is None:
            content: str | list[Any] = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = extract_text(raw_content)

        usage = getattr(message, "usage", None)
        usage_meta = None
        if usage is not None:
            usage_meta = {
                "input_tokens": getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }

        response_meta: dict[str, Any] = {}
        if hasattr(message, "model"):
            response_meta["model"] = message.model
        if hasattr(message, "finish_reason"):
            response_meta["finish_reason"] = message.finish_reason

        return cls(
            content=content,
            tool_calls=tool_calls,
            usage_metadata=usage_meta,
            response_metadata=response_meta or None,
        )

    @classmethod
    def from_lmstudio_assistant(
        cls,
        message: Any,
        *,
        prediction: Any | None = None,
    ) -> "PseudoAIMessage":
        """Build from ``lmstudio`` ``AssistantResponse`` (``TextData`` + ``ToolCallRequestData`` blocks)."""
        import json as _json

        tool_calls: list[PseudoToolCall] | None = None
        texts: list[str] = []
        for block in getattr(message, "content", ()) or ():
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(str(getattr(block, "text", "") or ""))
            elif btype == "toolCallRequest":
                tcr = getattr(block, "tool_call_request", None)
                if tcr is None:
                    continue
                name = str(getattr(tcr, "name", "") or "")
                tc_id = getattr(tcr, "id", None)
                args_obj = getattr(tcr, "arguments", {})
                if isinstance(args_obj, str):
                    arg_str = args_obj
                else:
                    arg_str = _json.dumps(args_obj, ensure_ascii=False)
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(
                    PseudoToolCall(
                        name=name,
                        args=arg_str,
                        id=str(tc_id) if tc_id is not None else None,
                    )
                )

        content: str | list[Any] = "\n".join(texts) if texts else ""

        usage_meta: dict[str, Any] | None = None
        if prediction is not None:
            stats = getattr(prediction, "stats", None)
            if stats is not None:
                inp = getattr(stats, "prompt_tokens_count", None)
                out_tok = getattr(stats, "predicted_tokens_count", None)
                tot = getattr(stats, "total_tokens_count", None)
                if inp is not None or out_tok is not None or tot is not None:
                    usage_meta = {
                        "input_tokens": inp,
                        "output_tokens": out_tok,
                        "total_tokens": tot,
                    }

        return cls(
            content=content,
            tool_calls=tool_calls,
            usage_metadata=usage_meta,
            response_metadata=None,
        )


def serialize_lc_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize LangChain AIMessage / ToolMessage objects for recording."""
    serial_msgs: list[dict[str, Any]] = []
    for m in messages:
        role = getattr(m, "type", "unknown")
        content = getattr(m, "content", None)
        entry: dict[str, Any] = {
            "role": role,
            "content": json_safe(content),
        }
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            if tool_calls and isinstance(tool_calls[0], dict):
                entry["tool_calls"] = json_safe(tool_calls)
            else:
                # Dataclass-style tool_calls (PseudoToolCall, LangChain ToolCall)
                serialized_tcs: list[dict[str, Any]] = []
                for tc in tool_calls:
                    tc_dict: dict[str, Any] = {
                        "name": getattr(tc, "name", ""),
                        "args": getattr(tc, "args", ""),
                    }
                    tc_id = getattr(tc, "id", None)
                    if tc_id is not None:
                        tc_dict["id"] = tc_id
                    serialized_tcs.append(tc_dict)
                entry["tool_calls"] = serialized_tcs
        tool_call_id = getattr(m, "tool_call_id", None)
        if tool_call_id is not None:
            entry["tool_call_id"] = tool_call_id
        serial_msgs.append(entry)
    return serial_msgs


def serialize_pseudo_message(m: Any) -> dict[str, Any]:
    """Serialize a PseudoAIMessage (or any duck-type with .content/.tool_calls) for recording."""
    content = getattr(m, "content", None)
    if content is None:
        content_str = ""
    elif isinstance(content, str):
        content_str = content
    else:
        content_str = json.dumps(content, ensure_ascii=False, default=json_default_with_decimals)

    entry: dict[str, Any] = {
        "role": getattr(m, "type", "assistant"),
        "content": content_str,
    }
    tool_calls = getattr(m, "tool_calls", None)
    if tool_calls is not None:
        entry["tool_calls"] = [
            {"name": tc.name, "args": tc.args, "id": tc.id} for tc in tool_calls
        ]
    return entry


def _image_redaction_note(byte_count: int, mime_type: str) -> str:
    """The stable placeholder text written in place of image base64 payloads."""
    return f"<image: {byte_count} bytes, {mime_type}>"


def image_redacted_block(byte_count: int, mime_type: str) -> dict[str, Any]:
    """Placeholder content block persisted instead of raw image data."""
    return {
        "type": "image_redacted",
        "note": _image_redaction_note(byte_count, mime_type),
    }


def _decoded_base64_size(b64: str) -> int:
    """Approximate decoded byte count of a base64 string (exact modulo padding)."""
    stripped = b64.rstrip("=")
    return max(0, (len(stripped) * 3) // 4)


def _redact_one_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Return a redacted replacement for *block* if it carries image data, else ``None``.

    Recognises:
    - OpenAI vision blocks: ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}}``
    - Anthropic vision blocks: ``{"type": "image", "source": {"type": "base64", "media_type": ..., "data": <b64>}}``
    """
    btype = block.get("type")
    if btype == "image_url":
        image_url = block.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            header, sep, b64 = url.partition(",")
            mime = header[len("data:"):].split(";", 1)[0] or "unknown"
            size = _decoded_base64_size(b64) if sep else 0
            return image_redacted_block(size, mime)
        return None
    if btype == "image":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            data = source.get("data")
            mime = str(source.get("media_type") or "unknown")
            size = _decoded_base64_size(data) if isinstance(data, str) else 0
            return image_redacted_block(size, mime)
        return None
    return None


def redact_image_blocks(payload: Any) -> Any:
    """Deep-copy *payload* with every image content block replaced by a placeholder.

    Walks dicts/lists recursively and swaps OpenAI ``image_url`` (data URL) and
    Anthropic ``image`` (base64 source) blocks for
    ``{"type": "image_redacted", "note": "<image: N bytes, mime>"}``. Everything
    else is returned structurally unchanged (new containers, same leaves), so the
    recorded ``model_invocations.request_payload`` keeps its shape while base64
    image data never reaches persistence. Safe on payloads with no images.
    """
    if isinstance(payload, dict):
        replacement = _redact_one_block(payload)
        if replacement is not None:
            return replacement
        return {key: redact_image_blocks(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_image_blocks(item) for item in payload]
    if isinstance(payload, tuple):
        return [redact_image_blocks(item) for item in payload]
    return payload


def _encode_image_b64(part: Any) -> str:
    import base64

    return base64.b64encode(part.data).decode("ascii")


def openai_image_blocks(image_parts: Any) -> list[dict[str, Any]]:
    """OpenAI Chat Completions vision blocks (data-URL ``image_url``) for ``image_parts``."""
    return [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{part.mime_type};base64,{_encode_image_b64(part)}"
            },
        }
        for part in image_parts
    ]


def anthropic_image_blocks(image_parts: Any) -> list[dict[str, Any]]:
    """Anthropic Messages API vision blocks (base64 ``source``) for ``image_parts``."""
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.mime_type,
                "data": _encode_image_b64(part),
            },
        }
        for part in image_parts
    ]


def build_anthropic_messages(
    system_prompt: str,
    user_prompt: str,
    image_parts: Any = None,
) -> list[dict[str, Any]]:
    """Build Anthropic API message format from system + user prompts.

    With ``image_parts`` (a sequence of :class:`~doyoutrade.models.base.ImagePart`),
    the user turn becomes a block array: image blocks first, then the text block
    (Anthropic's recommended ordering for vision prompts).
    """
    msgs: list[dict[str, Any]] = []
    if system_prompt:
        msgs.append({"role": "user", "content": system_prompt})
    if image_parts:
        content: list[dict[str, Any]] = anthropic_image_blocks(image_parts)
        content.append({"type": "text", "text": user_prompt})
        msgs.append({"role": "user", "content": content})
    else:
        msgs.append({"role": "user", "content": user_prompt})
    return msgs


def build_openai_messages(
    system_prompt: str,
    user_prompt: str,
    image_parts: Any = None,
) -> list[dict[str, Any]]:
    """Build OpenAI API message format from system + user prompts.

    With ``image_parts`` (a sequence of :class:`~doyoutrade.models.base.ImagePart`),
    the user turn becomes a block array: the text block first, then one
    ``image_url`` data-URL block per image.
    """
    msgs: list[dict[str, Any]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    if image_parts:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        content.extend(openai_image_blocks(image_parts))
        msgs.append({"role": "user", "content": content})
    else:
        msgs.append({"role": "user", "content": user_prompt})
    return msgs


def _lc_turn_role(msg: Any) -> str:
    """Normalize LangChain / internal message to ``human`` | ``assistant`` | ``tool`` | ``unknown``."""
    t = getattr(msg, "type", None)
    if isinstance(t, str):
        if t == "tool":
            return "tool"
        if t in ("human", "user"):
            return "human"
        if t in ("ai", "assistant"):
            return "assistant"
    r = getattr(msg, "role", None)
    if isinstance(r, str):
        if r == "tool":
            return "tool"
        if r in ("user", "human"):
            return "human"
        if r == "assistant":
            return "assistant"
    return "unknown"


def _assistant_blocks_for_anthropic(msg: Any) -> list[dict[str, Any]]:
    """Build Anthropic assistant ``content`` blocks (text + tool_use)."""
    blocks: list[dict[str, Any]] = []
    content = getattr(msg, "content", "")
    if isinstance(content, str) and content.strip():
        blocks.append({"type": "text", "text": content})
    for tc in getattr(msg, "tool_calls", None) or []:
        tc_id = getattr(tc, "id", None)
        if tc_id is None and isinstance(tc, dict):
            tc_id = tc.get("id")
        name = getattr(tc, "name", None)
        if name is None and isinstance(tc, dict):
            name = tc.get("name")
        args = getattr(tc, "args", None)
        if args is None and isinstance(tc, dict):
            args = tc.get("args", "{}")
        if isinstance(args, str):
            try:
                inp: Any = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                inp = {"_raw": args}
        elif isinstance(args, dict):
            inp = dict(args)
        else:
            inp = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(tc_id) if tc_id is not None else "",
                "name": str(name or ""),
                "input": inp,
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def build_anthropic_messages_from_lc_turns(messages: list[Any]) -> list[dict[str, Any]]:
    """Convert LangChain-style multi-turn messages (excluding system) to Anthropic Messages API ``messages``.

    Merges consecutive tool-result messages into a single ``user`` turn with multiple
    ``tool_result`` blocks, as required by the API after an assistant ``tool_use``.

    If a tool message carries ``companion_user_text`` (Claude Code–style skill injection),
    those strings are appended as ``text`` blocks in the **same** ``user`` turn after the
    ``tool_result`` blocks (same pattern as injecting skill content alongside tool results).
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []
    pending_companion_texts: list[str] = []

    def flush_tool_results() -> None:
        nonlocal pending_results, pending_companion_texts
        if pending_results:
            combined: list[dict[str, Any]] = list(pending_results)
            for extra in pending_companion_texts:
                combined.append({"type": "text", "text": extra})
            out.append({"role": "user", "content": combined})
            pending_results = []
            pending_companion_texts = []

    for msg in messages:
        kind = _lc_turn_role(msg)
        if kind == "tool":
            tid = getattr(msg, "tool_call_id", None)
            if tid is None:
                continue
            body = getattr(msg, "content", "")
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False, default=json_default_with_decimals)
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(tid),
                    "content": body,
                }
            )
            companion = getattr(msg, "companion_user_text", None)
            if isinstance(companion, str) and companion.strip():
                pending_companion_texts.append(companion.strip())
            continue

        flush_tool_results()

        if kind == "human":
            body = getattr(msg, "content", "")
            text = body if isinstance(body, str) else str(body)
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif kind == "assistant":
            out.append({"role": "assistant", "content": _assistant_blocks_for_anthropic(msg)})
        else:
            body = getattr(msg, "content", "")
            text = body if isinstance(body, str) else str(body)
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})

    flush_tool_results()
    return out


def anthropic_messages_api_params(
    messages: list[Any],
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    tools: list[dict[str, Any]] | None,
    thinking: dict[str, Any] | None,
    cache_control: dict[str, Any] | None,
) -> dict[str, Any]:
    """Kwargs for ``client.messages.create`` (Anthropic Messages API wire body)."""
    system, rest = _extract_system_from_messages(messages)
    anthropic_messages = build_anthropic_messages_from_lc_turns(rest)

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": anthropic_messages,
    }
    if system:
        params["system"] = system
    if temperature is not None:
        params["temperature"] = temperature
    if thinking is not None:
        params["thinking"] = thinking
    if cache_control is not None:
        params["cache_control"] = cache_control
    if tools:
        params["tools"] = tools
    return params


async def chat_ainvoke_anthropic(
    client: Any,
    messages: list[Any],
    tools: list[dict[str, Any]] | None,
    model: str,
    max_tokens: int,
    temperature: float | None,
    thinking: dict[str, Any] | None,
    cache_control: dict[str, Any] | None,
) -> ModelResponse:
    """Async tool-invocation call via Anthropic official SDK."""
    anthropic_tools = openai_function_tools_to_anthropic(tools) if tools else None
    params = anthropic_messages_api_params(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=anthropic_tools,
        thinking=thinking,
        cache_control=cache_control,
    )

    raw_resp = await client.messages.with_raw_response.create(**params)
    message = raw_resp.parse()
    wire_json: dict[str, Any] | None = None
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
        invocation_request_payload=recordable_request_params(params),
        invocation_response_payload=recordable_anthropic_sdk_response(message, wire_json),
    )
    log_model_response_debug(out, adapter="anthropic")
    return out


async def chat_ainvoke_openai(
    client: Any,
    messages: list[Any],
    tools: list[dict[str, Any]] | None,
    model: str,
    max_tokens: int | None,
    temperature: float | None,
) -> ModelResponse:
    """Async tool-invocation call via OpenAI official SDK."""
    # Convert LangChain-style messages to OpenAI format
    system, rest = _extract_system_from_messages(messages)
    openai_messages: list[dict[str, Any]] = []
    if system:
        openai_messages.append({"role": "system", "content": system})
    openai_messages.append({"role": "user", "content": _message_content_to_str(rest)})

    params: dict[str, Any] = {
        "model": model,
        "messages": openai_messages,
    }
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None and max_tokens > 0:
        params["max_tokens"] = max_tokens
    if tools:
        params["tools"] = tools

    response = await client.chat.completions.create(**params)
    chat_message = _first_chat_completion_message(response)
    raw = PseudoAIMessage.from_openai(chat_message)
    out = ModelResponse(
        text=extract_text(raw.content),
        raw=raw,
        invocation_request_payload=recordable_request_params(params),
        invocation_response_payload=recordable_sdk_response(response),
    )
    log_model_response_debug(out, adapter="openai_compatible")
    return out


def _extract_system_from_messages(
    messages: list[Any],
) -> tuple[str, list[Any]]:
    """Extract system prompt from LangChain-style message list."""
    system = ""
    rest: list[Any] = []
    for msg in messages:
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        content = getattr(msg, "content", "")
        if role in ("system", "SystemMessage"):
            system = content if isinstance(content, str) else str(content)
        else:
            rest.append(msg)
    return system, rest


def _message_content_to_str(messages: list[Any]) -> str:
    """Concatenate message content from LangChain-style messages."""
    parts: list[str] = []
    for msg in messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(block.get("text", str(block)))
        else:
            parts.append(str(content))
    return "\n".join(parts)


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, Iterable):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                value = item.get("text")
                if value is not None:
                    parts.append(str(value))
        if parts:
            return "\n".join(parts)

    return str(content)
