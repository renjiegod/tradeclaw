"""LM Studio local LLM provider (official ``lmstudio`` Python SDK)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from doyoutrade.config import _deep_merge
from doyoutrade.money.decimal_helpers import json_default_with_decimals
from doyoutrade.models.base import (
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    log_model_response_debug,
)
from doyoutrade.models.providers._common import (
    PseudoAIMessage,
    extract_text,
    recordable_request_params,
)
from doyoutrade.models.providers.openai_compatible import _tool_calls_to_openai_chat_format


def _require_lmstudio() -> Any:
    try:
        import lmstudio as lms
    except ImportError as exc:
        raise RuntimeError("lmstudio is not installed") from exc
    return lms


def _normalize_lmstudio_base_url(url: str | None) -> str | None:
    """Fix ``http//host`` / ``https//host`` (missing colon) so the SDK does not emit ``http://http//...``."""
    if url is None:
        return None
    s = url.strip()
    if not s:
        return None
    m = re.match(r"^(https?)//(?!/)", s, flags=re.IGNORECASE)
    if m:
        rest = s[m.end() :]
        return f"{m.group(1).lower()}://{rest}"
    return s


def _lmstudio_sdk_api_host(base_url: str | None) -> str | None:
    """Turn config ``base_url`` into the shape the ``lmstudio`` SDK expects: ``host:port`` without a scheme.

    The SDK builds probe URLs as ``http://{api_host}/lmstudio-greeting``. If ``api_host`` is already a
    full URL (e.g. ``http://localhost:1234``), the target becomes ``http://http://localhost:1234/...`` and
    requests fail or time out.
    """
    fixed = _normalize_lmstudio_base_url(base_url)
    if fixed is None:
        return None
    lower = fixed.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        parsed = urlparse(fixed)
        if parsed.netloc:
            return parsed.netloc
    return fixed


def openai_function_tools_to_lmstudio_raw_tools_dict(
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Map OpenAI Chat Completions ``tools`` entries to LM Studio ``rawTools.toolArray`` shape.

    Each input item is ``{\"type\":\"function\",\"function\":{name, description, parameters}}``.
    Output is ``{\"type\": \"toolArray\", \"tools\": [<LlmTool-shaped dicts>]}`` (wire-oriented dicts).
    """
    mapped: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if item.get("type") == "function" else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name", "") or "")
        desc = fn.get("description")
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}} if params is None else {}
        mapped.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc if isinstance(desc, str) or desc is None else str(desc),
                    "parameters": params,
                },
            }
        )
    return {"type": "toolArray", "tools": mapped}


def _langchain_messages_to_openai_dicts(messages: list[Any]) -> list[dict[str, Any]]:
    """Convert LangChain-style messages to OpenAI message dicts (same semantics as *OpenAICompatibleAdapter*)."""
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
            entry_t: dict[str, Any] = {"role": role, "content": str(content)}
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id:
                entry_t["tool_call_id"] = tc_id
            result.append(entry_t)
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


def _openai_assistant_tool_calls_to_lmstudio_requests(
    tool_calls: list[dict[str, Any]],
) -> list[Any]:
    from lmstudio._sdk_models import ToolCallRequest

    out: list[Any] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = str(fn.get("name", "") or "")
        tc_id = str(tc.get("id", "") or "")
        args_raw = fn.get("arguments", "{}")
        if isinstance(args_raw, str):
            try:
                parsed: Any = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"_raw": args_raw}
        elif isinstance(args_raw, dict):
            parsed = args_raw
        else:
            parsed = {}
        out.append(
            ToolCallRequest(
                type="function",
                name=name,
                id=tc_id,
                arguments=parsed,
            )
        )
    return out


def openai_role_dicts_to_lmstudio_chat(dicts: list[dict[str, Any]], lms: Any) -> Any:
    """Build ``lmstudio.history.Chat`` from OpenAI-style role dicts."""
    chat: Any = lms.Chat()
    for row in dicts:
        role = row.get("role", "")
        if role == "system":
            chat.add_system_prompt(str(row.get("content", "")))
        elif role == "user":
            chat.add_user_message(str(row.get("content", "")))
        elif role == "assistant":
            tcalls = row.get("tool_calls")
            if tcalls:
                reqs = _openai_assistant_tool_calls_to_lmstudio_requests(list(tcalls))
                chat.add_assistant_response(str(row.get("content", "") or ""), reqs)
            else:
                chat.add_assistant_response(str(row.get("content", "") or ""))
        elif role == "tool":
            tc_id = row.get("tool_call_id", "") or ""
            chat.add_tool_result({"tool_call_id": str(tc_id), "content": str(row.get("content", ""))})
        else:
            chat.add_user_message(str(row.get("content", "")))
    return chat


def _build_prediction_config_dict(
    *,
    tools: list[dict[str, Any]] | None,
    temperature: float | None,
    max_tokens: int | None,
    tool_choice: str | dict[str, Any] | None = None,
    prediction_config_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if temperature is not None:
        cfg["temperature"] = temperature
    if max_tokens is not None and max_tokens > 0:
        cfg["maxTokens"] = max_tokens
    if tools:
        raw_tools: dict[str, Any] = openai_function_tools_to_lmstudio_raw_tools_dict(tools)
        # LM Studio ``LlmToolUseSettingToolArray`` uses ``force`` (not OpenAI's ``tool_choice`` wire).
        if tool_choice == "required" or (
            isinstance(tool_choice, dict) and tool_choice.get("type") == "function"
        ):
            raw_tools["force"] = True
        cfg["rawTools"] = raw_tools
    if prediction_config_extra:
        cfg = _deep_merge(cfg, prediction_config_extra)
    return cfg


def _reraise_lmstudio_jinja_safe_hint(exc: BaseException) -> None:
    """LM Studio's Jinja runtime lacks the ``safe`` filter; models that use ``|safe`` in the chat
    template fail server-side (lmstudio-bug-tracker #1342). Re-raise with an operator hint while
    preserving ``LMStudioServerError`` for callers that branch on the type.
    """
    lower = str(exc).lower()
    if "unknown stringvalue filter: safe" not in lower:
        raise exc
    try:
        from lmstudio.json_api import LMStudioServerError
    except ImportError:
        raise exc
    if not isinstance(exc, LMStudioServerError):
        raise exc
    details = getattr(exc, "_raw_error", None)
    hint = (
        "\n\n[doyoutrade] LM Studio failed to render the model chat template: it uses Jinja's "
        "'|safe' filter, which this LM Studio build does not implement "
        "(https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1342). "
        "Fix: in LM Studio open the loaded model → Prompt Template and remove '|safe' "
        "(e.g. use '|tojson' instead of '|tojson|safe'). "
        "Alternatively set JSON key 'prediction_config_extra' on the model provider or route "
        "to pass a fixed promptTemplate in the LM Studio prediction config."
    )
    raise LMStudioServerError(str(exc) + hint, details) from exc


def _recordable_prediction_result(result: Any) -> dict[str, Any]:
    from msgspec import to_builtins

    try:
        payload = to_builtins(result)
    except TypeError:
        payload = {
            "content": getattr(result, "content", None),
            "parsed": getattr(result, "parsed", None),
            "stats": getattr(result, "stats", None),
            "model_info": getattr(result, "model_info", None),
            "structured": getattr(result, "structured", None),
            "load_config": getattr(result, "load_config", None),
            "prediction_config": getattr(result, "prediction_config", None),
        }
    return json.loads(json.dumps(payload, default=json_default_with_decimals))


class LmStudioAdapter(ModelAdapter):
    """LM Studio LLM via one ``respond`` call per *chat_ainvoke* (outer strategies run the tool loop)."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        prediction_config_extra: dict[str, Any] | None = None,
    ) -> None:
        _require_lmstudio()
        self.model = model
        self.api_key = api_key
        # ``lmstudio`` SDK parameter name is ``api_host``; config uses ``base_url`` (optional).
        # Accept ``http://host:port`` in YAML; the SDK needs ``host:port`` only (see ``_lmstudio_sdk_api_host``).
        self.api_host = _lmstudio_sdk_api_host(base_url)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.tool_choice = tool_choice
        self.prediction_config_extra = prediction_config_extra

    def generate(self, request: ModelRequest) -> ModelResponse:
        lms = _require_lmstudio()
        config = _build_prediction_config_dict(
            tools=request.tools,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            tool_choice=self.tool_choice,
            prediction_config_extra=self.prediction_config_extra,
        )

        chat = lms.Chat()
        if request.system_prompt:
            chat.add_system_prompt(request.system_prompt)
        chat.add_user_message(request.user_prompt)

        invocation_request_core = {
            "model": self.model,
            "history": chat._get_history(),
            "config": config,
        }

        with lms.Client(self.api_host) as client:
            llm = client.llm.model(self.model)
            prediction, assistant_msg = _sync_respond_collect_tools(llm, chat, config)

        raw = PseudoAIMessage.from_lmstudio_assistant(assistant_msg, prediction=prediction)
        text = str(getattr(prediction, "content", "") or "")
        if not text:
            text = extract_text(raw.content)
        out = ModelResponse(
            text=text,
            raw=raw,
            invocation_request_payload=recordable_request_params(invocation_request_core),
            invocation_response_payload=_recordable_prediction_result(prediction),
        )
        log_model_response_debug(out, adapter="lmstudio")
        return out

    def _lmstudio_chat_and_record(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Build LM Studio ``Chat`` and the dict persisted as ``request_payload`` (same as ``respond`` input)."""
        lms = _require_lmstudio()
        dicts = _langchain_messages_to_openai_dicts(messages)
        chat = openai_role_dicts_to_lmstudio_chat(dicts, lms)
        config = _build_prediction_config_dict(
            tools=tools,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            tool_choice=self.tool_choice,
            prediction_config_extra=self.prediction_config_extra,
        )
        record = {
            "model": self.model,
            "history": chat._get_history(),
            "config": config,
        }
        return chat, record

    def build_invocation_record_dict(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """SDK-shaped request passed to ``llm.respond`` (same structure as persisted ``request_payload``)."""
        _, record = self._lmstudio_chat_and_record(messages, tools)
        return record

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        lms = _require_lmstudio()
        chat, invocation_request_core = self._lmstudio_chat_and_record(messages, tools)
        config = invocation_request_core["config"]

        async with lms.AsyncClient(self.api_host) as client:
            llm = await client.llm.model(self.model)
            prediction, assistant_msg = await _async_respond_collect_tools(llm, chat, config)

        text = str(getattr(prediction, "content", "") or "")
        raw = PseudoAIMessage.from_lmstudio_assistant(assistant_msg, prediction=prediction)
        if not text:
            text = extract_text(raw.content)
        out = ModelResponse(
            text=text,
            raw=raw,
            invocation_request_payload=recordable_request_params(invocation_request_core),
            invocation_response_payload=_recordable_prediction_result(prediction),
        )
        log_model_response_debug(out, adapter="lmstudio")
        return out

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_text_delta=None,
    ):
        from doyoutrade.agent_runtime import agent_turn_response_from_model_response

        if on_text_delta is None:
            return agent_turn_response_from_model_response(
                await self.chat_ainvoke(messages, tools=tools)
            )

        lms = _require_lmstudio()
        chat, invocation_request_core = self._lmstudio_chat_and_record(messages, tools)
        config = invocation_request_core["config"]

        async with lms.AsyncClient(self.api_host) as client:
            llm = await client.llm.model(self.model)
            prediction, assistant_msg = await _async_respond_collect_tools(
                llm,
                chat,
                config,
                on_text_delta=on_text_delta,
            )

        text = str(getattr(prediction, "content", "") or "")
        raw = PseudoAIMessage.from_lmstudio_assistant(assistant_msg, prediction=prediction)
        if not text:
            text = extract_text(raw.content)
        out = ModelResponse(
            text=text,
            raw=raw,
            invocation_request_payload=recordable_request_params(invocation_request_core),
            invocation_response_payload=_recordable_prediction_result(prediction),
        )
        log_model_response_debug(out, adapter="lmstudio")
        turn = agent_turn_response_from_model_response(
            out
        )
        return turn


def _sync_respond_collect_tools(llm: Any, chat: Any, config: dict[str, Any]) -> tuple[Any, Any]:
    from lmstudio.json_api import PredictionToolCallEvent

    stream = llm.respond_stream(chat, config=config)
    tool_requests: list[Any] = []
    try:
        for event in stream._iter_events():
            if isinstance(event, PredictionToolCallEvent):
                tool_requests.append(event.arg)
        prediction = stream.result()
    except BaseException as exc:
        _reraise_lmstudio_jinja_safe_hint(exc)
    assistant_msg = chat.add_assistant_response(prediction, tool_requests)
    return prediction, assistant_msg


async def _async_respond_collect_tools(
    llm: Any,
    chat: Any,
    config: dict[str, Any],
    *,
    on_text_delta: Any = None,
) -> tuple[Any, Any]:
    from lmstudio.json_api import PredictionToolCallEvent

    stream = await llm.respond_stream(chat, config=config)
    tool_requests: list[Any] = []
    try:
        async for event in stream._iter_events():
            if isinstance(event, PredictionToolCallEvent):
                tool_requests.append(event.arg)
                continue
            text = _text_delta_from_lmstudio_event(event)
            if text and on_text_delta is not None:
                maybe_awaitable = on_text_delta(text)
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable
        prediction = stream.result()
    except BaseException as exc:
        _reraise_lmstudio_jinja_safe_hint(exc)
    assistant_msg = chat.add_assistant_response(prediction, tool_requests)
    return prediction, assistant_msg


def _text_delta_from_lmstudio_event(event: Any) -> str:
    for attr in ("content", "text", "delta"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""
