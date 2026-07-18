from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

try:  # Prefer real LangChain message classes when installed.
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
except Exception:  # pragma: no cover - dependency fallback for stripped test envs
    from doyoutrade.test_messages import AIMessage, HumanMessage, ToolMessage

from doyoutrade.assistant.attachments import compose_model_user_text
from doyoutrade.assistant.repository import (
    InMemoryAssistantRepository,
    normalize_tool_configs,
)
from doyoutrade.assistant.prompt_templates import resolve_agent_system_prompt
from doyoutrade.assistant.context_compaction.full import (
    build_full_compaction_plan,
    build_summary_boundary_metadata,
    build_summary_generation_messages,
    generate_compaction_summary,
    history_rows_after_latest_boundary,
)
from doyoutrade.assistant.context_compaction.service import (
    evaluate_full_compaction,
    prepare_messages_for_model,
)
from doyoutrade.assistant.context_compaction.types import normalize_context_compaction_config
from doyoutrade.assistant.lifecycle_commands import parse_lifecycle_command
from doyoutrade.assistant.slash_commands import resolve_skill_command_key, build_skill_invocation_message
from doyoutrade.assistant.skill_preload import build_preloaded_skills_prompt
from doyoutrade.assistant.main_agent import builtin_skill_names, is_main_agent
from doyoutrade.tools import OperationRegistry, build_default_tool_registry
from doyoutrade.assistant.approvals import (
    APPROVAL_ALLOWLIST_CONFIG_KEY,
    DEFAULT_APPROVAL_RULES,
    ApprovalBroker,
    ApprovalRule,
    match_approval_rule,
)
from doyoutrade.assistant.questions import QuestionBroker, QuestionResolution
from doyoutrade.assistant.channels.base import ChannelDeliveryHandle
from doyoutrade.skills.flow import (
    Flow,
    FlowError,
    advance_flow,
    build_flow_reminder_text,
    parse_choice,
    extract_flow_from_skill_body,
    strip_choice_tags,
)
from doyoutrade.skills.loader import load_skills
from doyoutrade.config import get_config
from doyoutrade.agent_runtime import (
    AgentToolCall,
    AgentTurnResponse,
    agent_turn_response_from_model_response,
)
from doyoutrade.models.invocation_context import model_invocation_scope
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.observability.debug_span_export import debug_span_export_for_session
from doyoutrade.persistence.errors import RecordNotFoundError

logger = get_logger(__name__)
tracer = get_tracer(__name__)

ModelAdapterFactory = Callable[[str | None], Awaitable[Any]]
_DEFAULT_SESSION_TITLE = "DoYouTrade Agent"
_CHANNEL_PLACEHOLDER_TITLE_RE = re.compile(r"^Session [A-Za-z0-9:_-]+$")
_TOOL_INVENTORY_STATE_KEY = "tool_inventory_state"
_CHANNEL_DELIVERY_STATE_KEY = "channel_delivery_state"
_CHANNEL_DELIVERY_REF_LIMIT = 128
_CHANNEL_DELIVERY_TEXT_LIMIT = 4000


def _discover_tools_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "discover_tools",
            "description": (
                "Inspect deferred tools allowed for this agent and optionally activate them "
                "for the current session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional substring filter over deferred tool names and descriptions.",
                    },
                    "activate": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Deferred tool names to activate for the current session.",
                    },
                },
                "required": [],
            },
        },
    }


def _unique_names(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = str(name or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _truncate_channel_delivery_text(text: str) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= _CHANNEL_DELIVERY_TEXT_LIMIT:
        return normalized
    return normalized[: _CHANNEL_DELIVERY_TEXT_LIMIT - 3] + "..."


class _AssistantRunError(Exception):
    """Wraps an exception from _run_loop to carry trace_payload + partial output.

    A mid-run failure (e.g. a streaming ``httpx.ReadTimeout``) used to discard the
    work already streamed/executed this run: only ``trace_payload`` escaped the span
    context, so ``_run_and_finalize`` had nothing to persist and the chat showed just
    the user's query. We now also ferry out the partial assistant text, content blocks,
    tool events, and thinking blocks so the failure path can persist a visible
    partial/error assistant message (mirroring the user-stop path).
    """

    def __init__(
        self,
        trace_payload: dict[str, Any],
        exc: Exception,
        *,
        partial_text: str = "",
        content_blocks: list[dict[str, Any]] | None = None,
        tool_events: list[dict[str, Any]] | None = None,
        thinking_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(str(exc))
        self.trace_payload = trace_payload
        self.partial_text = partial_text
        self.content_blocks = list(content_blocks or [])
        self.tool_events = list(tool_events or [])
        self.thinking_blocks = list(thinking_blocks or [])
        self.__cause__ = exc


class AssistantStoppedError(Exception):
    """Raised when user actively stops the assistant during a run."""

    def __init__(
        self,
        *,
        partial_text: str = "",
        metadata: dict[str, Any] | None = None,
        trace_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("Assistant stopped by user")
        self.partial_text = partial_text
        self.metadata = dict(metadata or {})
        self.trace_payload = dict(trace_payload or {})


def _content_blocks_with_partial_text(
    content_blocks: list[dict[str, Any]], partial_text: str
) -> list[dict[str, Any]]:
    """Return ``content_blocks`` with a synthesized partial text block inserted.

    Used by both the user-stop and the run-failure partial-persistence paths so a
    bubble shows the streamed text alongside any tool calls. The text block is
    inserted before the first ``tool_call`` (or appended when there is none) unless
    an identical text block already exists.
    """
    blocks = list(content_blocks)
    if partial_text and not any(
        block.get("type") == "text" and block.get("content") == partial_text
        for block in blocks
    ):
        text_block = {"type": "text", "content": partial_text}
        first_tool_index = next(
            (index for index, block in enumerate(blocks) if block.get("type") == "tool_call"),
            None,
        )
        if first_tool_index is None:
            blocks.append(text_block)
        else:
            blocks.insert(first_tool_index, text_block)
    return blocks


# Exception class names (httpx + anthropic SDK) that represent a transient transport
# failure worth one model-call retry. Matched by name across the cause/context chain
# so we don't hard-import httpx/anthropic here and still catch wrapped timeouts. HTTP
# status errors (4xx/5xx) are deliberately excluded — they are not retried at this layer.
_RETRYABLE_MODEL_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "ReadError",
        "ConnectError",
        "RemoteProtocolError",
        "APITimeoutError",
        "APIConnectionError",
    }
)


def _is_retryable_model_transport_error(exc: BaseException) -> bool:
    """True for transient transport failures (timeouts / dropped connections).

    Walks the ``__cause__`` / ``__context__`` chain because the failure that reaches
    the assistant loop is often a wrapped form (e.g. an ``httpx.ReadTimeout`` surfaced
    through the anthropic SDK). :class:`AssistantStoppedError` is not in the set, so a
    user-initiated stop is never treated as retryable.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _RETRYABLE_MODEL_TRANSPORT_ERROR_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


@dataclass(frozen=True)
class _AssistantCycleState:
    task_id: str | None
    run_id: str
    trace_id: str | None


@dataclass(frozen=True)
class _ResolvedOperationHandlers:
    base_tool_names: list[str]
    deferred_tool_names: list[str]
    activated_deferred_tool_names: list[str]
    effective_tool_names: list[str]
    registry: OperationRegistry


class _StreamingControllerBatcher:
    """Batch same-type streaming updates before sending them to a channel controller."""

    def __init__(self, streaming_controller: Any, *, max_items: int = 5, max_delay_sec: float = 0.3) -> None:
        self._controller = streaming_controller
        self._max_items = max(1, max_items)
        self._max_delay_sec = max(0.0, max_delay_sec)
        self._lock = asyncio.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_sent: dict[str, str] = {}

    async def publish(self, event_type: str, value: str) -> None:
        if not value:
            return
        async with self._lock:
            if self._last_sent.get(event_type) == value:
                return
            pending = self._pending.get(event_type)
            if pending is None:
                pending = {"count": 0, "value": "", "task": None}
                self._pending[event_type] = pending
            pending["count"] += 1
            pending["value"] = value
            if pending["task"] is None or pending["task"].done():
                pending["task"] = asyncio.create_task(self._flush_later(event_type))
            if pending["count"] >= self._max_items:
                await self._flush_locked(event_type)

    async def flush_all(self) -> None:
        async with self._lock:
            for event_type in list(self._pending):
                await self._flush_locked(event_type)

    async def _flush_later(self, event_type: str) -> None:
        try:
            await asyncio.sleep(self._max_delay_sec)
            async with self._lock:
                await self._flush_locked(event_type)
        except asyncio.CancelledError:
            pass

    async def _flush_locked(self, event_type: str) -> None:
        pending = self._pending.get(event_type)
        if pending is None or not pending["value"]:
            return
        task = pending.get("task")
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if task is not asyncio.current_task():
            pending["task"] = None
        value = str(pending["value"])
        pending["count"] = 0
        pending["value"] = ""
        self._last_sent[event_type] = value
        if event_type == "text":
            await self._controller.on_partial_reply(value)
        elif event_type == "thinking":
            await self._controller.on_reasoning_stream(value)


def _current_span_payload() -> dict[str, str | None]:
    from opentelemetry import trace

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return {"trace_id": None, "span_id": None}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def _json_loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return dict(parsed) if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _tool_result_is_error(result: Any) -> bool:
    # Prefer the explicit side-channel attached by ``_ToolResultStr`` /
    # ``ToolResult``. Falls back to JSON sniffing so legacy tools that still
    # return ``_json_dumps({"status": "error", ...})`` keep being flagged.
    flag = getattr(result, "is_error", None)
    if flag is not None:
        return bool(flag)
    if isinstance(result, str) and result.startswith("[error"):
        return True
    parsed = _json_loads_object(result)
    status = str(parsed.get("status", "")).lower()
    return status == "error" or bool(parsed.get("is_error"))


def _tool_call_parts(tool_call: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(tool_call, AgentToolCall):
        return tool_call.name, tool_call.id or f"tc-{uuid4().hex[:8]}", dict(tool_call.arguments)
    if isinstance(tool_call, dict):
        name = str(tool_call.get("name") or tool_call.get("function", {}).get("name") or "")
        call_id = str(tool_call.get("id") or f"tc-{uuid4().hex[:8]}")
        raw_args = tool_call.get("args")
        if raw_args is None:
            raw_args = tool_call.get("function", {}).get("arguments")
        return name, call_id, _json_loads_object(raw_args)
    name = str(getattr(tool_call, "name", ""))
    call_id = str(getattr(tool_call, "id", None) or f"tc-{uuid4().hex[:8]}")
    return name, call_id, _json_loads_object(getattr(tool_call, "args", None))


def _tool_call_for_message(tool_call: Any) -> dict[str, Any]:
    name, call_id, args = _tool_call_parts(tool_call)
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _message_content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(value)


def _is_compact_command(text: str) -> bool:
    return text.strip().lower() == "/compact"


def _should_generate_session_title(title: Any) -> bool:
    normalized = str(title or "").strip()
    if not normalized:
        return True
    if normalized == _DEFAULT_SESSION_TITLE:
        return True
    return _CHANNEL_PLACEHOLDER_TITLE_RE.fullmatch(normalized) is not None


def _build_system_prompt(
    tool_registry: OperationRegistry,
    agent=None,
) -> str:
    base = (
        "你是 DoYouTrade 的研究与策略 Agent。你可以帮助用户梳理交易想法、加载 skills、"
        "生成 DoYouTrade 策略定义与组合图，并通过 doyoutrade 自身 backtest task 运行回测。\n\n"
        "硬性约束：\n"
        "- 策略和回测必须使用 doyoutrade 自身的策略/回测体系，不要假装已经运行外部回测。\n"
        "- 涉及金额、费用、数量×价格时，对外使用十进制字符串。\n"
        "- 如果需要完整 skill 指南，调用 load_skill；优先通过 strategy 工具创建 definition / graph / task binding；"
        "如需跑回测，统一调用 run_strategy_backtest（支持 task_id 模式跑现有任务，或 definition_id 模式自动建任务+跑+等终态）。\n"
        f"- 当前可用工具：{', '.join(tool_registry.names)}。\n"
    )

    # 拼接预加载 skills
    if agent is not None:
        skill_names = agent.get("skill_names") if isinstance(agent, dict) else getattr(agent, "skill_names", None)
        if skill_names:
            preload = build_preloaded_skills_prompt(list(skill_names))
            if preload:
                return base + "\n\n" + preload

    return base

def _compose_effective_system_prompt(
    tool_registry: OperationRegistry,
    agent: dict[str, Any] | None,
) -> str:
    custom_prompt = resolve_agent_system_prompt(agent) if agent is not None else ""
    base_prompt = custom_prompt or _build_system_prompt(tool_registry, agent=agent)
    # The fixed main agent advertises EVERY enabled skill (code-controlled),
    # expanded here at compose time rather than from the DB row. load_skills
    # scans the directory, so this only runs when composing a system prompt —
    # never on the hot list_agents path.
    if agent is not None and is_main_agent(agent):
        skill_names: list[str] | None = builtin_skill_names()
    else:
        skill_names = agent.get("skill_names") if agent else None
    if skill_names:
        preload = build_preloaded_skills_prompt(list(skill_names))
        if preload:
            return base_prompt + "\n\n" + preload
    return base_prompt


def _conversation_messages_from_rows(rows: list[dict[str, Any]], fallback_user_text: str) -> list[Any]:
    messages: list[Any] = []
    for row in history_rows_after_latest_boundary(rows):
        role = row.get("role")
        content = str(row.get("content") or "")
        if role == "user":
            # Persisted content is the user's own text only; re-inject the
            # structured attachments as the model-visible path block so the
            # agent can still read_file them on later turns. Must use the same
            # composer as the live turn so the reconstructed last message equals
            # ``fallback_user_text`` (otherwise the tail check below duplicates it).
            metadata = row.get("metadata")
            atts = metadata.get("attachments") if isinstance(metadata, dict) else None
            messages.append(HumanMessage(content=compose_model_user_text(content, atts)))
        elif role == "assistant":
            # A run that failed mid-stream persists a flagged assistant message so the
            # failure stays visible in the chat, but it must NOT be replayed into the
            # model's history — otherwise the synthesized "本轮运行失败…" notice (or a
            # truncated partial) is fed back as something the assistant actually said.
            metadata = row.get("metadata")
            if isinstance(metadata, dict) and metadata.get("failed"):
                continue
            messages.append(AIMessage(content=content))
    if not messages or getattr(messages[-1], "content", None) != fallback_user_text:
        messages.append(HumanMessage(content=fallback_user_text))
    return messages


_RUNTIME_CONTEXT_TZ = timezone(timedelta(hours=8))


def _build_runtime_context_reminder_message() -> HumanMessage:
    """Build a per-attempt runtime-context reminder (Asia/Shanghai, UTC+8).

    Mirrors ClaudeCode's ``prependUserContext`` pattern (`src/utils/api.ts`):
    feed real-time facts the system prompt cannot carry as a separate
    user-role ``<system-reminder>`` message, so the model doesn't have to
    spend a tool call asking for the wall clock.
    """

    # Resolve the private knowledge-base root to an absolute path here so the
    # model never has to guess the server's $HOME. The in-process file
    # primitives reject ``~`` (not absolute), so feeding the expanded path
    # avoids a wasted ``echo $HOME`` round-trip and the wrong-home failure mode.
    from doyoutrade.tools._sandbox import knowledge_root

    kb_root = knowledge_root().expanduser()

    now = datetime.now(_RUNTIME_CONTEXT_TZ)
    text = (
        "<system-reminder>\n"
        "As you answer the user's questions, you can use the following context:\n"
        f"# currentDate\nToday's date is {now.strftime('%Y-%m-%d')} (Asia/Shanghai, UTC+8).\n"
        f"# currentTime\n{now.strftime('%Y-%m-%d %H:%M:%S')} +08:00\n"
        f"# currentWeekday\n{now.strftime('%A')}\n"
        f"# knowledgeBase\nThe user's private knowledge base is at {kb_root} "
        "(absolute path). Use this exact path with the file tools — they require "
        "an absolute path and do not expand '~'.\n"
        "</system-reminder>"
    )
    return HumanMessage(content=text)


def _inject_runtime_context_reminder(history_messages: list[Any]) -> list[Any]:
    """Return ``history_messages`` with a runtime-context reminder inserted
    immediately before the latest user message.

    Inserted in-memory only — the reminder is never persisted to history
    rows. Placing it adjacent to the tail (instead of at index 0) keeps
    the prefix cacheable across turns: only the last user message and
    its reminder miss the cache when the timestamp rolls forward.
    """

    reminder = _build_runtime_context_reminder_message()
    if not history_messages:
        return [reminder]
    return [*history_messages[:-1], reminder, history_messages[-1]]


def _inject_turn_context_reminder(
    history_messages: list[Any],
    reminder_text: str | None,
) -> list[Any]:
    """Insert a one-turn reminder immediately before the tail user message."""
    text = str(reminder_text or "").strip()
    if not text:
        return history_messages
    reminder = HumanMessage(content=text)
    if not history_messages:
        return [reminder]
    return [*history_messages[:-1], reminder, history_messages[-1]]


def _history_contains_summary_boundary(history_rows: list[dict[str, Any]]) -> bool:
    """Return True if compaction has run at any point in this session.

    Reads the boundary marker that
    ``doyoutrade/assistant/context_compaction/full.py::build_summary_boundary_metadata``
    stamps on the summary message it inserts into the history. The row
    field is ``metadata`` (see ``_message_dict`` in
    ``doyoutrade/assistant/repository.py``) which is sourced from the
    ``metadata_json`` column on ``AssistantMessageRecord``.
    """

    for row in history_rows:
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        ctx_meta = meta.get("context_compaction") or {}
        if isinstance(ctx_meta, dict) and ctx_meta.get("kind") == "summary_boundary":
            return True
    return False


async def _inject_loaded_skills_reminder(
    history_messages: list[Any],
    *,
    session_id: str,
    history_rows: list[dict[str, Any]],
    loaded_skill_repository: Any,
) -> list[Any]:
    """Insert a ``<system-reminder>`` HumanMessage carrying loaded-skill
    content when this session has been compacted at least once.

    Returns ``history_messages`` unchanged when:

    * the loaded-skill repository wasn't wired into ``AssistantService``,
    * no ``summary_boundary`` marker is in history (the original
      ``tool_result`` for ``load_skill`` is still present, so re-injecting
      would waste tokens), or
    * the reminder builder returned ``None`` (no rows / repo error —
      already logged in :mod:`doyoutrade.assistant.loaded_skills_reminder`).

    The reminder is inserted just before the tail user message — same
    convention as :func:`_inject_runtime_context_reminder`. Callers
    typically run this *before* injecting the per-turn runtime-context
    reminder so the runtime-context reminder ends up closest to the tail.
    """

    if loaded_skill_repository is None:
        return history_messages
    if not _history_contains_summary_boundary(history_rows):
        return history_messages

    from doyoutrade.assistant.loaded_skills_reminder import (
        build_loaded_skills_reminder,
    )

    reminder = await build_loaded_skills_reminder(session_id, loaded_skill_repository)
    if reminder is None:
        return history_messages

    if not history_messages:
        return [reminder]
    return [*history_messages[:-1], reminder, history_messages[-1]]


class AssistantService:
    # Number of times a single model call is retried within the same turn when it
    # fails with a transient transport error (timeout / dropped connection), BEFORE
    # any tool is dispatched. Tools are not idempotent (execute_bash side effects,
    # task/cron/strategy mutations), so we never retry a whole turn — only the model
    # call that produced no tool dispatch yet. 1 = two total attempts.
    _model_transport_max_retries: int = 1

    def __init__(
        self,
        repository: Any | None = None,
        *,
        agent_repository: Any | None = None,
        platform_service: Any | None = None,
        strategy_registry_service: Any | None = None,
        strategy_definition_repository: Any | None = None,
        model_adapter_factory: ModelAdapterFactory | None = None,
        tool_registry: OperationRegistry | None = None,
        loaded_skill_repository: Any | None = None,
        job_watch_repository: Any | None = None,
        run_repository: Any | None = None,
        decision_signal_repository: Any | None = None,
        instrument_catalog_repository: Any | None = None,
        knowledge_graph_repository: Any | None = None,
        approval_rules: list[ApprovalRule] | tuple[ApprovalRule, ...] | None = None,
        max_turns: int = 6,
    ):
        self.repository = repository or InMemoryAssistantRepository()
        self.agent_repo = agent_repository
        self.platform_service = platform_service
        self.strategy_registry_service = strategy_registry_service
        self.strategy_definition_repository = strategy_definition_repository
        self.model_adapter_factory = model_adapter_factory
        # Persists ``load_skill`` calls so the next turn (post-compaction)
        # can re-inject the SKILL.md body via a ``<system-reminder>``. T3
        # consumes this repo to build the reminder; here we just plumb it
        # into the default tool registry so ``LoadSkillTool`` can write.
        self._loaded_skill_repository = loaded_skill_repository
        self.tool_registry = tool_registry or build_default_tool_registry(
            tool_result_max_chars=get_config().assistant.tool_result_max_chars,
            loaded_skill_repository=loaded_skill_repository,
            assistant_repository=self.repository,
            job_watch_repository=job_watch_repository,
            run_repository=run_repository,
            decision_signal_repository=decision_signal_repository,
            instrument_catalog_repository=instrument_catalog_repository,
            knowledge_graph_repository=knowledge_graph_repository,
            model_adapter_factory=model_adapter_factory,
        )
        self.max_turns = max(1, int(max_turns))
        # Blocking tool-call approvals (WireHookHandle-style): rules decide
        # which calls suspend on a pending future; channels / the web API
        # resolve through ``self.approval_broker``.
        self.approval_rules: tuple[ApprovalRule, ...] = (
            tuple(approval_rules) if approval_rules is not None else DEFAULT_APPROVAL_RULES
        )
        self.approval_broker = ApprovalBroker()
        # Blocking ask_user_question waits (same future-broker skeleton as
        # approvals): a call suspends inside its execution slot until a card
        # click / free-typed reply resolves the future through
        # ``self.question_broker``. The answer is fed back as the tool_result
        # and the SAME run continues — no synthetic user message.
        self.question_broker = QuestionBroker()
        self._abort_events: dict[str, asyncio.Event] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _get_abort_event(self, session_id: str) -> asyncio.Event:
        if session_id not in self._abort_events:
            self._abort_events[session_id] = asyncio.Event()
        return self._abort_events[session_id]

    def _clear_abort_event(self, session_id: str) -> None:
        if session_id in self._abort_events:
            self._abort_events[session_id].clear()

    # ---------------------------------------------------------------- flow
    # Flow-skill runtime: ``session.config["active_flow"]`` holds
    # ``{skill_name, node_id, invalid_choice, started_at}`` (written by
    # LoadSkillTool). Per attempt we inject a <system-reminder> describing
    # the current node; when the model ends a reply with <choice>...</choice>
    # the attempt-completion path advances the node. Every transition (and
    # every breakage) is persisted as a ``flow.*`` assistant event.

    async def _resolve_flow_definition(self, session_id: str, skill_name: str) -> Flow | None:
        """Parse the flow graph for *skill_name*, preferring the body
        persisted at load time (stable mid-flow even if the SKILL.md on
        disk changes) and falling back to the on-disk skill registry.

        Returns None when no body can be found; raises FlowError when a
        body exists but its flowchart is invalid.
        """
        body: str | None = None
        if self._loaded_skill_repository is not None:
            try:
                rows = await self._loaded_skill_repository.list_by_session(session_id)
            except Exception as exc:
                logger.warning(
                    "flow.loaded_body_read_failed session_id=%s skill_name=%s err=%s: %s",
                    session_id,
                    skill_name,
                    type(exc).__name__,
                    exc,
                )
                rows = []
            for row in rows:
                if row.get("skill_name") == skill_name:
                    candidate = row.get("body")
                    if isinstance(candidate, str) and candidate.strip():
                        body = candidate
                    break
        if body is None:
            for skill in load_skills(enabled_only=True):
                if skill.name == skill_name:
                    body = skill.body
                    break
        if body is None:
            return None
        return extract_flow_from_skill_body(body)

    async def _abort_active_flow(
        self,
        session_id: str,
        *,
        skill_name: str,
        node_id: str,
        reason: str,
        hint: str,
        attempt_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Clear flow state with full visibility (event + ERROR log)."""
        logger.error(
            "flow.aborted session_id=%s skill_name=%s node_id=%s reason=%s hint=%s",
            session_id,
            skill_name,
            node_id,
            reason,
            hint,
        )
        try:
            await self.repository.update_session_config(session_id, {"active_flow": None})
        except Exception as exc:
            logger.error(
                "flow.abort_state_clear_failed session_id=%s err=%s: %s",
                session_id,
                type(exc).__name__,
                exc,
            )
        await self.repository.append_event(
            session_id=session_id,
            event_type="flow.aborted",
            payload={
                "attempt_id": attempt_id,
                "run_id": run_id,
                "skill_name": skill_name,
                "node_id": node_id,
                "reason": reason,
                "hint": hint,
                **_current_span_payload(),
            },
        )

    async def _inject_active_flow_reminder(
        self,
        session: dict[str, Any],
        history_messages: list[Any],
        *,
        attempt_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Any]:
        """Insert the current flow node's <system-reminder> before the tail
        user message (same convention as the runtime-context reminder)."""
        config = dict(session.get("config") or {})
        state = config.get("active_flow")
        if not isinstance(state, dict) or not state:
            return history_messages
        session_id = str(session.get("session_id") or "")
        skill_name = str(state.get("skill_name") or "")
        node_id = str(state.get("node_id") or "")
        try:
            flow = await self._resolve_flow_definition(session_id, skill_name)
        except FlowError as exc:
            await self._abort_active_flow(
                session_id,
                skill_name=skill_name,
                node_id=node_id,
                reason="flow_parse_error",
                hint=f"persisted flow body no longer parses: {exc}",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            return history_messages
        if flow is None:
            await self._abort_active_flow(
                session_id,
                skill_name=skill_name,
                node_id=node_id,
                reason="skill_body_missing",
                hint="neither assistant_loaded_skills nor the skills root has this skill",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            return history_messages
        invalid_choice = state.get("invalid_choice")
        text = build_flow_reminder_text(
            skill_name=skill_name,
            flow=flow,
            node_id=node_id,
            invalid_choice=str(invalid_choice) if invalid_choice else None,
        )
        if text is None:
            await self._abort_active_flow(
                session_id,
                skill_name=skill_name,
                node_id=node_id,
                reason="node_missing",
                hint="recorded node_id is gone from the flowchart (skill edited mid-flow?)",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            return history_messages
        reminder = HumanMessage(content=text)
        if not history_messages:
            return [reminder]
        return [*history_messages[:-1], reminder, history_messages[-1]]

    # ------------------------------------------------------------ ask_user
    # ``ask_user_question`` blocks (fizz-style): the tool call suspends inside
    # its execution slot on a ``QuestionBroker`` future; a card click / answer
    # endpoint / free-typed reply resolves it and the answer is fed back as the
    # tool_result, continuing the SAME run. Answers therefore never create a
    # synthetic user message or a new attempt. Audit via ``user_question.*``.

    async def _answer_pending_question_from_message(
        self, session: dict[str, Any], text: str
    ) -> dict[str, Any] | None:
        """Route an answer that arrived as a chat message (legacy ``/ask_user``
        protocol from Feishu cards, or a free-typed reply while a question is
        pending) to the suspended tool wait. Returns a light envelope when it
        resolved / consumed a live wait; ``None`` when the message is not an
        answer or no live wait exists (caller handles it as a normal message)."""
        session_id = str(session.get("session_id") or "")
        config = dict(session.get("config") or {})
        pending = config.get("pending_user_question")
        pending = pending if isinstance(pending, dict) and pending else None

        if text.startswith("/ask_user"):
            parts = text.split(maxsplit=2)
            question_id = parts[1] if len(parts) > 1 else ""
            answer = parts[2].strip() if len(parts) > 2 else ""
            # Legacy multi-select option labels were joined by 、.
            selected = [s for s in (p.strip() for p in answer.split("、")) if s]
            custom = "" if selected else answer
            source = "option_click"
        elif pending is not None:
            question_id = str(pending.get("question_id") or "")
            selected = []
            custom = text.strip()
            source = "free_text"
        else:
            return None

        request = self.question_broker.get(question_id) if question_id else None
        if request is None:
            # No live suspended wait: only intercept the explicit /ask_user
            # protocol (a stale card click) — surface it, don't silently start a
            # turn. A plain free-typed message falls through to normal handling.
            if not text.startswith("/ask_user"):
                return None
            logger.info(
                "user_question stale answer (no live wait) session_id=%s question_id=%s",
                session_id,
                question_id,
            )
            await self.repository.append_event(
                session_id=session_id,
                event_type="user_question.stale_answer",
                payload={
                    "question_id": question_id,
                    "answer": custom or "、".join(selected),
                    "pending_question_id": (pending or {}).get("question_id"),
                    "hint": "card click arrived with no live suspended wait (process restarted?)",
                },
            )
            if pending is not None and question_id == str(pending.get("question_id") or ""):
                await self._clear_pending_user_question(
                    session_id,
                    question_id=question_id,
                    resolution="stale_answer",
                    source=source,
                    selected=selected,
                    custom=custom,
                )
            return {
                "session": session,
                "messages": [],
                "resolved_user_question": {
                    "question_id": question_id,
                    "accepted": False,
                    "reason": "no_live_wait",
                },
            }

        accepted = self.question_broker.resolve(
            question_id, selected=selected, custom=custom, source=source
        )
        return {
            "session": session,
            "messages": [],
            "resolved_user_question": {
                "question_id": question_id,
                "accepted": accepted,
                "source": source,
            },
        }

    async def _clear_pending_user_question(
        self,
        session_id: str,
        *,
        question_id: str,
        resolution: str,
        source: str,
        selected: list[str] | None = None,
        custom: str = "",
    ) -> None:
        try:
            await self.repository.update_session_config(
                session_id, {"pending_user_question": None}
            )
        except Exception as exc:
            logger.error(
                "user_question.state_clear_failed session_id=%s question_id=%s err=%s: %s",
                session_id,
                question_id,
                type(exc).__name__,
                exc,
            )
        await self.repository.append_event(
            session_id=session_id,
            event_type=f"user_question.{resolution}",
            payload={
                "question_id": question_id,
                "source": source,
                "selected": list(selected or []),
                "custom": custom,
            },
        )

    async def _publish_user_question(
        self,
        session_id: str,
        *,
        attempt_id: str,
        run_id: str,
        content_blocks: list[dict[str, Any]],
        streaming_controller: Any,
    ) -> None:
        """After a successful ask_user_question tool call: surface the
        pending question as a content block (web UI renders option buttons
        from it), an assistant event, and — when the channel supports it —
        an interactive card via the streaming controller."""
        try:
            session = await self.repository.get_session(session_id)
        except Exception as exc:
            logger.error(
                "user_question.publish_session_read_failed session_id=%s err=%s: %s",
                session_id,
                type(exc).__name__,
                exc,
            )
            return
        pending = ((session or {}).get("config") or {}).get("pending_user_question")
        if not isinstance(pending, dict) or not pending:
            logger.error(
                "user_question.publish_missing_state session_id=%s attempt_id=%s "
                "(tool reported success but pending_user_question is absent)",
                session_id,
                attempt_id,
            )
            return
        block = {
            "type": "user_question",
            "question_id": pending.get("question_id"),
            "question": pending.get("question"),
            "header": pending.get("header"),
            "options": pending.get("options"),
            "multi_select": pending.get("multi_select"),
        }
        content_blocks.append(block)
        await self.repository.append_event(
            session_id=session_id,
            event_type="user_question.asked",
            payload={
                "attempt_id": attempt_id,
                "run_id": run_id,
                "question_id": pending.get("question_id"),
                "question": pending.get("question"),
                "header": pending.get("header"),
                # Full option list so the web SSE handler can render the live
                # card while the turn is suspended (the persisted content block
                # only lands when the turn finishes).
                "options": pending.get("options"),
                "multi_select": pending.get("multi_select"),
                "option_count": len(pending.get("options") or []),
                **_current_span_payload(),
            },
        )
        if streaming_controller is not None:
            on_user_question = getattr(streaming_controller, "on_user_question", None)
            if on_user_question is not None:
                try:
                    await on_user_question(pending)
                except Exception as exc:
                    # Channel delivery failing must be visible — the user
                    # would otherwise wait on a question they never saw.
                    logger.error(
                        "user_question.channel_delivery_failed session_id=%s "
                        "question_id=%s err=%s: %s",
                        session_id,
                        pending.get("question_id"),
                        type(exc).__name__,
                        exc,
                    )
                    await self.repository.append_event(
                        session_id=session_id,
                        event_type="user_question.delivery_failed",
                        payload={
                            "attempt_id": attempt_id,
                            "run_id": run_id,
                            "question_id": pending.get("question_id"),
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "hint": "interactive card send failed; the web content block still renders",
                        },
                    )

    async def _await_user_question_answer(
        self,
        session_id: str,
        *,
        attempt_id: str,
        run_id: str,
        content_blocks: list[dict[str, Any]],
        streaming_controller: Any,
    ) -> str:
        """Publish the pending question, then suspend on a broker future until
        a card click / answer endpoint / free-typed reply resolves it. Returns
        the structured answer as this tool call's ``tool_result`` (or a
        structured timeout error). Runs inside the dispatch loop's tool slot so
        the abort race cancels the wait on a user stop.

        The card MUST be published *before* awaiting (else the user can't answer
        a card they can't see), and the broker future MUST exist *before* the
        card is delivered (else a fast answer would miss it) — hence the
        create → publish → wait order below.
        """
        try:
            session = await self.repository.get_session(session_id)
        except Exception as exc:
            logger.error(
                "user_question.await_session_read_failed session_id=%s err=%s: %s",
                session_id,
                type(exc).__name__,
                exc,
            )
            return (
                "[error:user_question_await_failed] 无法读取会话状态来呈现问题；本次提问未生效。\n"
                "hint: 直接在正文里用文字问用户。"
            )
        pending = ((session or {}).get("config") or {}).get("pending_user_question")
        if not isinstance(pending, dict) or not pending:
            logger.error(
                "user_question.await_missing_state session_id=%s attempt_id=%s "
                "(tool reported success but pending_user_question is absent)",
                session_id,
                attempt_id,
            )
            return (
                "[error:user_question_await_failed] 待回答的问题状态缺失，无法等待答案；本次提问未生效。\n"
                "hint: 直接在正文里用文字问用户。"
            )

        question_id = str(pending.get("question_id") or "")
        # Broker future first, so a resolve arriving the instant the card shows
        # cannot slip past an un-registered wait.
        request = self.question_broker.create(
            question_id=question_id,
            session_id=session_id,
            attempt_id=attempt_id,
            run_id=run_id,
            question=str(pending.get("question") or ""),
            header=pending.get("header"),
            options=list(pending.get("options") or []),
            multi_select=bool(pending.get("multi_select")),
        )
        # Publish (content block for web live render + user_question.asked event
        # + interactive channel card).
        await self._publish_user_question(
            session_id,
            attempt_id=attempt_id,
            run_id=run_id,
            content_blocks=content_blocks,
            streaming_controller=streaming_controller,
        )

        try:
            resolution = await request.wait()
        finally:
            self.question_broker.discard(question_id)

        if resolution.timed_out:
            await self._clear_pending_user_question(
                session_id,
                question_id=question_id,
                resolution="timeout",
                source="timeout",
            )
            logger.warning(
                "user_question timed out question_id=%s after %.0fs",
                question_id,
                request.timeout_seconds,
            )
            return (
                "[error:user_question_timeout] 用户未在规定时间内作答，问题已取消。\n"
                "hint: 用一句话告诉用户提问超时；不要重复提问，等他主动回复后再继续。"
            )

        # Record the answer as a read-only recap on the published content block
        # so a page reload rebuilds it (fizz-style in-card recap), and clear the
        # pending state with a visible user_question.answered event.
        for block in content_blocks:
            if (
                block.get("type") == "user_question"
                and block.get("question_id") == question_id
            ):
                block["answered"] = True
                block["selected"] = list(resolution.selected)
                block["custom"] = resolution.custom or None
                block["answer_source"] = resolution.source
                break
        await self._clear_pending_user_question(
            session_id,
            question_id=question_id,
            resolution="answered",
            source=resolution.source,
            selected=list(resolution.selected),
            custom=resolution.custom,
        )
        logger.info(
            "user_question answered question_id=%s source=%s selected=%s custom=%s",
            question_id,
            resolution.source,
            resolution.selected,
            bool(resolution.custom),
        )
        answer_obj = {
            "selected": list(resolution.selected),
            "custom": resolution.custom or None,
            "source": resolution.source,
        }
        return json.dumps(answer_obj, ensure_ascii=False)

    # ----------------------------------------------------------- approvals
    # Blocking approvals: a matching tool call suspends inside its execution
    # slot until a human resolves the pending future (Feishu card button /
    # web banner / timeout). Runs INSIDE the dispatch loop's abort race, so
    # a user stop cancels the wait cleanly.

    async def _gate_tool_call_for_approval(
        self,
        *,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        attempt_id: str,
        run_id: str,
        streaming_controller: Any = None,
    ) -> str | None:
        """Return a structured error string when the call is blocked
        (rejected / timed out), or None when execution may proceed."""
        rule = match_approval_rule(self.approval_rules, tool_name, arguments)
        if rule is None:
            return None

        # Session-remembered "approve always" — visible, never silent.
        try:
            session = await self.repository.get_session(session_id)
        except Exception as exc:
            logger.error(
                "approval allowlist read failed session_id=%s err=%s: %s "
                "(failing closed: approval will be required)",
                session_id,
                type(exc).__name__,
                exc,
            )
            session = None
        allowlist = list(
            ((session or {}).get("config") or {}).get(APPROVAL_ALLOWLIST_CONFIG_KEY) or []
        )
        command_preview = str(arguments.get("command") or "") or json.dumps(
            arguments, ensure_ascii=False, default=str
        )
        if rule.allow_always and rule.key in allowlist:
            await self.repository.append_event(
                session_id=session_id,
                event_type="approval.auto_approved",
                payload={
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "tool": tool_name,
                    "rule_key": rule.key,
                    "command_preview": command_preview[:500],
                    **_current_span_payload(),
                },
            )
            logger.info(
                "approval auto-approved session_id=%s rule=%s tool=%s",
                session_id,
                rule.key,
                tool_name,
            )
            return None

        request = self.approval_broker.create(
            session_id=session_id,
            attempt_id=attempt_id,
            run_id=run_id,
            tool_name=tool_name,
            rule=rule,
            command_preview=command_preview,
        )
        await self.repository.append_event(
            session_id=session_id,
            event_type="approval.requested",
            payload={**request.payload(), **_current_span_payload()},
        )
        logger.info(
            "approval requested approval_id=%s session_id=%s rule=%s tool=%s",
            request.approval_id,
            session_id,
            rule.key,
            tool_name,
        )
        if streaming_controller is not None:
            on_approval_request = getattr(streaming_controller, "on_approval_request", None)
            if on_approval_request is not None:
                try:
                    await on_approval_request(request.payload())
                except Exception as exc:
                    # The card failing must be visible — the operator would
                    # otherwise stare at a silently suspended turn. The wait
                    # continues: the web banner / API can still resolve.
                    logger.error(
                        "approval card delivery failed approval_id=%s err=%s: %s",
                        request.approval_id,
                        type(exc).__name__,
                        exc,
                    )
                    await self.repository.append_event(
                        session_id=session_id,
                        event_type="approval.delivery_failed",
                        payload={
                            "approval_id": request.approval_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "hint": "interactive card send failed; resolve via web banner or API",
                        },
                    )

        try:
            resolution = await request.wait()
        finally:
            self.approval_broker.discard(request.approval_id)

        resolved_payload = {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "approval_id": request.approval_id,
            "tool": tool_name,
            "rule_key": rule.key,
            "action": resolution.action,
            "source": resolution.source,
            "resolver_id": resolution.resolver_id,
            **_current_span_payload(),
        }
        if resolution.action == "timeout":
            await self.repository.append_event(
                session_id=session_id, event_type="approval.timeout", payload=resolved_payload
            )
            logger.warning(
                "approval timed out approval_id=%s rule=%s after %.0fs",
                request.approval_id,
                rule.key,
                request.timeout_seconds,
            )
            return (
                f"[error:approval_timeout] 操作待审批超时（{int(request.timeout_seconds)} 秒内"
                f"无人响应）：{rule.description}。该操作未执行。\n"
                "hint: 告诉用户审批超时、操作未执行；待用户明确同意后再重试一次。"
            )
        await self.repository.append_event(
            session_id=session_id, event_type="approval.resolved", payload=resolved_payload
        )
        logger.info(
            "approval resolved approval_id=%s action=%s source=%s",
            request.approval_id,
            resolution.action,
            resolution.source,
        )
        if resolution.action == "reject":
            return (
                f"[error:approval_rejected] 用户拒绝了该操作：{rule.description}。"
                "该操作未执行。\n"
                "hint: 不要立即重试同一命令；向用户说明操作已被拒绝，并询问是否需要"
                "调整方案。"
            )
        if resolution.action == "approve_always" and not rule.allow_always:
            logger.warning(
                "approval always rejected for one-time rule approval_id=%s rule=%s",
                request.approval_id,
                rule.key,
            )
            return (
                "[error:approval_always_forbidden] 该操作必须逐次人工审批，"
                "不支持“总是允许”。本次操作未执行。"
            )
        if resolution.action == "approve_always":
            merged = sorted({*allowlist, rule.key})
            try:
                await self.repository.update_session_config(
                    session_id, {APPROVAL_ALLOWLIST_CONFIG_KEY: merged}
                )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="approval.remembered",
                    payload={
                        "approval_id": request.approval_id,
                        "rule_key": rule.key,
                        "allowlist": merged,
                    },
                )
            except Exception as exc:
                # Remembering failing only degrades to asking again next
                # time — but it must be visible, not swallowed.
                logger.error(
                    "approval allowlist write failed session_id=%s rule=%s err=%s: %s",
                    session_id,
                    rule.key,
                    type(exc).__name__,
                    exc,
                )
        return None

    async def _maybe_advance_flow(
        self,
        session_id: str,
        final_text: str,
        *,
        attempt_id: str | None,
        run_id: str | None,
        span: Any = None,
    ) -> str:
        """Advance the active flow from the model's final reply.

        Returns *final_text* with <choice> tags stripped whenever a tag was
        consumed (the tag is flow control, not user-facing prose). Without
        an active flow or without a tag, the text passes through unchanged.
        """
        if parse_choice(final_text) is None:
            return final_text
        try:
            session = await self.repository.get_session(session_id)
        except Exception as exc:
            logger.error(
                "flow.advance_session_read_failed session_id=%s err=%s: %s",
                session_id,
                type(exc).__name__,
                exc,
            )
            return final_text
        config = dict((session or {}).get("config") or {})
        state = config.get("active_flow")
        if not isinstance(state, dict) or not state:
            # A <choice> tag with no active flow: leave the text untouched
            # (could be the model quoting the syntax in conversation).
            return final_text
        skill_name = str(state.get("skill_name") or "")
        node_id = str(state.get("node_id") or "")
        stripped = strip_choice_tags(final_text)
        try:
            flow = await self._resolve_flow_definition(session_id, skill_name)
        except FlowError as exc:
            await self._abort_active_flow(
                session_id,
                skill_name=skill_name,
                node_id=node_id,
                reason="flow_parse_error",
                hint=f"persisted flow body no longer parses: {exc}",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            return stripped
        if flow is None:
            await self._abort_active_flow(
                session_id,
                skill_name=skill_name,
                node_id=node_id,
                reason="skill_body_missing",
                hint="neither assistant_loaded_skills nor the skills root has this skill",
                attempt_id=attempt_id,
                run_id=run_id,
            )
            return stripped

        outcome = advance_flow(flow, node_id, final_text)
        base_payload = {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "skill_name": skill_name,
            "from_node": node_id,
            "choice": outcome.choice,
            **_current_span_payload(),
        }
        if span is not None:
            span.set_attribute("assistant.flow.skill", skill_name)
            span.set_attribute("assistant.flow.status", outcome.status)
        try:
            if outcome.status == "advanced":
                await self.repository.update_session_config(
                    session_id,
                    {
                        "active_flow": {
                            **state,
                            "node_id": outcome.next_node_id,
                            "invalid_choice": None,
                        }
                    },
                )
                if span is not None:
                    span.set_attribute("assistant.flow.node", str(outcome.next_node_id))
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="flow.advanced",
                    payload={**base_payload, "to_node": outcome.next_node_id},
                )
                logger.info(
                    "flow.advanced session_id=%s skill=%s %s->%s choice=%r",
                    session_id,
                    skill_name,
                    node_id,
                    outcome.next_node_id,
                    outcome.choice,
                )
            elif outcome.status == "completed":
                await self.repository.update_session_config(
                    session_id, {"active_flow": None}
                )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="flow.completed",
                    payload={**base_payload, "to_node": outcome.next_node_id},
                )
                logger.info(
                    "flow.completed session_id=%s skill=%s last_node=%s",
                    session_id,
                    skill_name,
                    node_id,
                )
            elif outcome.status == "aborted":
                await self.repository.update_session_config(
                    session_id, {"active_flow": None}
                )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="flow.aborted",
                    payload={**base_payload, "reason": "model_choice"},
                )
                logger.info(
                    "flow.aborted session_id=%s skill=%s node=%s (model chose abort)",
                    session_id,
                    skill_name,
                    node_id,
                )
            elif outcome.status == "invalid":
                await self.repository.update_session_config(
                    session_id,
                    {"active_flow": {**state, "invalid_choice": outcome.choice}},
                )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="flow.choice_invalid",
                    payload={
                        **base_payload,
                        "reason": outcome.reason,
                        "hint": "next attempt's reminder lists the valid choices",
                    },
                )
                logger.info(
                    "flow.choice_invalid session_id=%s skill=%s node=%s choice=%r",
                    session_id,
                    skill_name,
                    node_id,
                    outcome.choice,
                )
            elif outcome.status == "broken":
                await self._abort_active_flow(
                    session_id,
                    skill_name=skill_name,
                    node_id=node_id,
                    reason="node_missing",
                    hint=str(outcome.reason or "recorded node vanished from flowchart"),
                    attempt_id=attempt_id,
                    run_id=run_id,
                )
        except Exception as exc:
            # State write / event append failed: the turn must still finish,
            # but the operator has to see the flow got stuck.
            logger.error(
                "flow.advance_persist_failed session_id=%s skill=%s node=%s err=%s: %s",
                session_id,
                skill_name,
                node_id,
                type(exc).__name__,
                exc,
            )
        return stripped

    def _spawn_background_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def aclose(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("assistant background task failed during shutdown", exc_info=True)
        await self.tool_registry.aclose()

    async def stop_attempt(self, session_id: str) -> dict[str, Any]:
        """外部调用：设置 abort event，触发正在运行的 attempt 停止"""
        session = await self.repository.get_session(session_id)
        was_running = session is not None and str(session.get("status") or "") == "running"
        abort_event = self._get_abort_event(session_id)
        abort_event.set()
        logger.info(
            "assistant stop requested session_id=%s was_running=%s",
            session_id,
            was_running,
        )
        return {"stopped": True, "active": was_running}

    async def create_session(
        self,
        *,
        agent_id: str,
        title: str = "",
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.agent_repo is not None:
            agent = await self.agent_repo.get_agent(agent_id)
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")
            if agent["status"] != "active":
                raise ValueError("Agent is inactive")
        else:
            agent = None
        initial_session = {"config": {}}
        resolved_tools = self._resolve_tool_inventory(initial_session, agent)
        template_id = None
        if agent is not None:
            template_id = str(
                agent.get("prompt_template_id")
                or agent.get("system_prompt_template_id")
                or ""
            ).strip() or None
        # Caller-supplied config seeds the session's settings; we then
        # merge in our agent / template defaults below. Used by cron
        # to stamp ``cron_origin=True`` so the API can block recursive
        # cron creation from that session.
        session_config: dict[str, Any] = dict(config or {})
        if not template_id:
            # No template linked → freeze the agent's raw system_prompt at
            # session creation so later agent edits don't reshape this
            # session. Template-backed sessions intentionally omit the
            # snapshot so attempts re-render the .j2 on every turn (see
            # the `system_prompt_snapshot` fallback in this file).
            session_config["system_prompt_snapshot"] = _compose_effective_system_prompt(
                resolved_tools.registry,
                agent,
            )
        else:
            session_config["system_prompt_template_id"] = template_id
            session_config["prompt_template_id"] = template_id
        session = await self.repository.create_session(
            agent_id=agent_id,
            title=title.strip(),
            session_id=session_id,
            config=session_config,
        )
        await self.repository.append_event(
            session_id=session["session_id"],
            event_type="session.created",
            payload={"session_id": session["session_id"], "title": session["title"], "agent_id": agent_id},
        )
        return session

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        channel_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        items, total = await self.repository.list_sessions(
            limit=limit,
            offset=offset,
            channel_id=channel_id,
            source=source,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self.repository.get_session(session_id)

    async def update_session_config(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        updater = getattr(self.repository, "update_session_config", None)
        if updater is None:
            raise AttributeError("assistant repository does not support update_session_config")
        return await updater(session_id, patch)

    async def resolve_channel_delivery_ref(
        self,
        session_id: str,
        *,
        channel_type: str,
        platform_message_id: str,
    ) -> dict[str, Any] | None:
        session = await self.repository.get_session(session_id)
        if session is None:
            return None
        config = dict(session.get("config") or {})
        state = dict(config.get(_CHANNEL_DELIVERY_STATE_KEY) or {})
        refs = list(state.get("refs") or [])
        normalized_channel_type = str(channel_type or "").strip()
        normalized_platform_message_id = str(platform_message_id or "").strip()
        if not normalized_channel_type or not normalized_platform_message_id:
            return None
        for ref in reversed(refs):
            if not isinstance(ref, dict):
                continue
            if str(ref.get("channel_type") or "").strip() != normalized_channel_type:
                continue
            if str(ref.get("platform_message_id") or "").strip() != normalized_platform_message_id:
                continue
            return dict(ref)
        return None

    async def register_channel_delivery_refs(
        self,
        session_id: str,
        *,
        channel_type: str,
        handles: list[ChannelDeliveryHandle],
        canonical_text: str,
        source: str,
        assistant_message_id: str = "",
    ) -> dict[str, Any] | None:
        normalized_channel_type = str(channel_type or "").strip()
        normalized_text = _truncate_channel_delivery_text(canonical_text)
        if not normalized_channel_type or not normalized_text:
            return None
        normalized_handles: list[ChannelDeliveryHandle] = []
        for handle in handles or []:
            platform_message_id = str(getattr(handle, "platform_message_id", "") or "").strip()
            if not platform_message_id:
                continue
            normalized_handles.append(handle)
        if not normalized_handles:
            return None

        session = await self.repository.get_session(session_id)
        if session is None:
            return None
        config = dict(session.get("config") or {})
        state = dict(config.get(_CHANNEL_DELIVERY_STATE_KEY) or {})
        refs = [
            dict(item)
            for item in list(state.get("refs") or [])
            if isinstance(item, dict)
        ]
        now_iso = _utcnow().isoformat()
        dedup: dict[tuple[str, str], dict[str, Any]] = {}
        for ref in refs:
            key = (
                str(ref.get("channel_type") or "").strip(),
                str(ref.get("platform_message_id") or "").strip(),
            )
            if not key[0] or not key[1]:
                continue
            dedup[key] = ref
        for handle in normalized_handles:
            platform_message_id = str(handle.platform_message_id or "").strip()
            key = (normalized_channel_type, platform_message_id)
            dedup[key] = {
                "channel_type": normalized_channel_type,
                "platform_message_id": platform_message_id,
                "platform_message_type": str(handle.platform_message_type or "").strip(),
                "canonical_text": normalized_text,
                "source": str(source or "").strip(),
                "assistant_message_id": str(assistant_message_id or "").strip(),
                "created_at": now_iso,
                "extra": dict(handle.extra or {}),
            }
        merged_refs = sorted(
            dedup.values(),
            key=lambda item: str(item.get("created_at") or ""),
        )[-_CHANNEL_DELIVERY_REF_LIMIT :]
        return await self.update_session_config(
            session_id,
            {_CHANNEL_DELIVERY_STATE_KEY: {"refs": merged_refs}},
        )

    async def get_active_channel_peer_session(
        self, channel_id: str, peer_session_id: str
    ) -> str | None:
        """Return the persisted active session a channel peer is rebound to (``/new``), if any."""
        getter = getattr(self.repository, "get_active_peer_session", None)
        if getter is None:
            return None
        return await getter(channel_id, peer_session_id)

    async def set_active_channel_peer_session(
        self, channel_id: str, peer_session_id: str, active_session_id: str
    ) -> None:
        """Persist a channel peer's active-session rebinding so it survives restarts."""
        setter = getattr(self.repository, "set_active_peer_session", None)
        if setter is None:
            raise AttributeError("assistant repository does not support set_active_peer_session")
        await setter(channel_id, peer_session_id, active_session_id)

    async def get_or_create_session(
        self,
        session_id: str,
        *,
        agent_id: str,
        title: str = "",
    ) -> dict[str, Any]:
        existing = await self.repository.get_session(session_id)
        if existing is not None:
            # Backfill agent_id if the existing session has NULL (e.g., created before agent_id column was added)
            if not existing.get("agent_id"):
                existing = await self.repository.update_session(session_id, agent_id=agent_id)
            return existing
        logger.info("creating new assistant session session_id=%s title=%s agent_id=%s", session_id, title, agent_id)
        session = await self.create_session(agent_id=agent_id, title=title, session_id=session_id)
        return session

    async def list_messages(self, session_id: str, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return await self.repository.list_messages(session_id, limit=limit, offset=offset)

    async def append_summary_boundary_message(
        self,
        session_id: str,
        *,
        content: str,
        linked_attempt_id: str | None,
        compacted_until_message_id: str,
        source_message_count: int,
    ) -> dict[str, Any]:
        return await self.repository.append_message(
            session_id=session_id,
            role="assistant",
            content=content,
            linked_attempt_id=linked_attempt_id,
            metadata=build_summary_boundary_metadata(
                compacted_until_message_id=compacted_until_message_id,
                source_message_count=source_message_count,
            ),
        )

    async def record_compaction_state(
        self,
        session_id: str,
        *,
        summary_message_id: str,
        compacted_until_message_id: str,
        raw_message_count_at_compaction: int,
    ) -> dict[str, Any]:
        session = await self.repository.get_session(session_id)
        if session is None:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")
        current_state = dict((session.get("config") or {}).get("context_compaction_state") or {})
        compaction_count = int(current_state.get("compaction_count") or 0) + 1
        current_state.update(
            {
                "last_compacted_at": _utcnow().isoformat(),
                "compaction_count": compaction_count,
                "summary_message_id": summary_message_id,
                "compacted_until_message_id": compacted_until_message_id,
                "raw_message_count_at_compaction": int(raw_message_count_at_compaction),
            }
        )
        return await self.update_session_config(
            session_id,
            {"context_compaction_state": current_state},
        )

    async def list_events(
        self,
        session_id: str,
        *,
        after_id: str | None = None,
        limit: int = 100,
        tail: bool = False,
    ) -> list[dict[str, Any]]:
        return await self.repository.list_events(session_id, after_id=after_id, limit=limit, tail=tail)

    async def list_traces(self, session_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        items, total = await self.repository.list_traces(session_id, limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def get_trace_detail(self, trace_id: str) -> dict[str, Any] | None:
        return await self.repository.get_trace_detail(trace_id)

    async def get_spans_for_sessions(self, session_ids: list[str]) -> dict[str, Any]:
        return await self.repository.get_spans_for_sessions(session_ids)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tool_registry.list_tools()

    def _tool_registry_for_names(self, tool_names: list[str]) -> OperationRegistry:
        tools = []
        for name in tool_names:
            tool = self.tool_registry.get(name)
            if tool is not None:
                tools.append(tool)
        return OperationRegistry(tools, tool_result_max_chars=getattr(self.tool_registry, "_tool_result_max_chars", 50000))

    def _resolve_tool_inventory(self, session: dict[str, Any], agent: dict[str, Any] | None) -> _ResolvedOperationHandlers:
        # The fixed main agent gets the FULL in-process tool registry (all base),
        # code-controlled rather than read from the DB row — same shape as the
        # "no agent" case. Free: reuses the already-built self.tool_registry.
        if agent is None or is_main_agent(agent):
            effective_tool_names = list(self.tool_registry.names)
            return _ResolvedOperationHandlers(
                base_tool_names=effective_tool_names,
                deferred_tool_names=[],
                activated_deferred_tool_names=[],
                effective_tool_names=effective_tool_names,
                registry=self._tool_registry_for_names(effective_tool_names),
            )

        tool_configs = normalize_tool_configs(
            agent.get("tool_configs"),
            fallback_tool_names=agent.get("tool_names"),
        )
        base_tool_names = [cfg["name"] for cfg in tool_configs if cfg.get("load_mode") == "base"]
        deferred_tool_names = [cfg["name"] for cfg in tool_configs if cfg.get("load_mode") == "deferred"]
        session_cfg = dict(session.get("config") or {})
        inventory_state = dict(session_cfg.get(_TOOL_INVENTORY_STATE_KEY) or {})
        activated_deferred_tool_names = _unique_names(
            [
                name
                for name in list(inventory_state.get("activated_deferred_tool_names") or [])
                if name in deferred_tool_names
            ]
        )
        effective_tool_names = _unique_names(base_tool_names + activated_deferred_tool_names)
        return _ResolvedOperationHandlers(
            base_tool_names=base_tool_names,
            deferred_tool_names=deferred_tool_names,
            activated_deferred_tool_names=activated_deferred_tool_names,
            effective_tool_names=effective_tool_names,
            registry=self._tool_registry_for_names(effective_tool_names),
        )

    def _tool_definitions_for_model(self, resolved: _ResolvedOperationHandlers) -> list[dict[str, Any]]:
        ordered_tool_names = _unique_names(
            resolved.activated_deferred_tool_names
            + [name for name in resolved.effective_tool_names if name not in resolved.activated_deferred_tool_names]
        )
        definitions = self._tool_registry_for_names(ordered_tool_names).definitions()
        if resolved.deferred_tool_names:
            return [_discover_tools_schema(), *definitions]
        return definitions

    async def _execute_discover_tools(
        self,
        *,
        session_id: str,
        args: dict[str, Any],
        resolved: _ResolvedOperationHandlers,
        attempt_id: str,
        run_id: str,
    ) -> str:
        query = str(args.get("query") or "").strip().lower()
        requested_activate = _unique_names([str(name) for name in list(args.get("activate") or [])])
        activated = [name for name in requested_activate if name in resolved.deferred_tool_names]
        if activated:
            activated_state = _unique_names(resolved.activated_deferred_tool_names + activated)
            await self.update_session_config(
                session_id,
                {
                    _TOOL_INVENTORY_STATE_KEY: {
                        "activated_deferred_tool_names": activated_state,
                    }
                },
            )
            await self.repository.append_event(
                session_id=session_id,
                event_type="tool_inventory.deferred_activated",
                payload={
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "activated_deferred_tool_names": activated,
                    "effective_tool_names": _unique_names(resolved.base_tool_names + activated_state),
                    **_current_span_payload(),
                },
            )

        deferred_items = []
        for name in resolved.deferred_tool_names:
            tool = self.tool_registry.get(name)
            if tool is None:
                continue
            description = str(getattr(tool, "description", "") or "")
            if query and query not in name.lower() and query not in description.lower():
                continue
            deferred_items.append(
                {
                    "name": name,
                    "description": description,
                    "category": getattr(tool, "category", "agent"),
                    "load_mode": "deferred",
                    "active": name in _unique_names(resolved.activated_deferred_tool_names + activated),
                }
            )
        return json.dumps(
            {
                "status": "ok",
                "base_tool_names": resolved.base_tool_names,
                "deferred_tools": deferred_items,
                "activated_deferred_tool_names": _unique_names(resolved.activated_deferred_tool_names + activated),
            },
            ensure_ascii=False,
        )

    async def send_message(
        self,
        *,
        session_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
        streaming_controller: Any = None,
        source_attribution: Mapping[str, str | None] | None = None,
        turn_context_reminder: str | None = None,
        user_message_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = await self.repository.get_session(session_id)
        if session is None:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")

        attachments = list(attachments or [])
        text = content.strip()

        # ask_user_question answers resolve the suspended tool wait (fizz-style
        # tool_result) and continue the SAME run — they never start a new
        # attempt or create a synthetic user message. Covers card clicks that
        # still arrive as the legacy ``/ask_user`` protocol (e.g. Feishu) and
        # free-typed replies while a question is pending. If no live wait
        # exists (process restarted, or the turn already ended) this returns
        # None and the message falls through to the normal path.
        answered = await self._answer_pending_question_from_message(session, text)
        if answered is not None:
            return answered

        if _is_compact_command(text):
            return await self._handle_compact_command(session)

        lifecycle_command = parse_lifecycle_command(text)
        if lifecycle_command is not None:
            return await self._handle_lifecycle_command(session, lifecycle_command.name)

        # 尝试 slash command 拦截
        cmd_key = resolve_skill_command_key(text)
        invocation_msg = None
        if cmd_key is not None:
            user_instruction = text[len(f"/{cmd_key}"):].strip() or None
            invocation_msg = build_skill_invocation_message(cmd_key, user_instruction)
            if invocation_msg:
                # 用 skill 内容替换原始 slash 命令文本
                text = invocation_msg
            else:
                # skill 找不到，当作普通消息处理
                cmd_key = None

        if not text and not attachments:
            raise ValueError("content is required")

        # Persisted user content stays clean (the user's own text only); the
        # structured attachments ride in metadata. The model-visible turn text
        # injects the absolute paths so the agent can read_file them — this is
        # never persisted into ``content`` nor shown to the user.
        message_metadata: dict[str, Any] = dict(user_message_metadata or {})
        if attachments:
            message_metadata["attachments"] = attachments
        model_text = compose_model_user_text(text, attachments)

        attempt_id = f"attempt-{uuid4().hex[:12]}"
        run_id = f"asst-run-{uuid4().hex[:12]}"
        self._clear_abort_event(session_id)
        await self.repository.update_session(session_id, status="running", last_attempt_id=attempt_id)
        user_message = await self.repository.append_message(
            session_id=session_id,
            role="user",
            content=text,
            linked_attempt_id=attempt_id,
            metadata=message_metadata,
        )
        await self.repository.append_event(
            session_id=session_id,
            event_type="message.created",
            payload={
                "message_id": user_message["message_id"],
                "role": "user",
                "attempt_id": attempt_id,
                "metadata_keys": sorted(message_metadata.keys()),
                "attachment_count": len(attachments),
            },
        )
        should_generate_title = _should_generate_session_title(session.get("title", ""))
        if should_generate_title:
            self._spawn_background_task(self._generate_title_background(session_id, text))

        # Run the model loop + finalization in a tracked background task so client
        # disconnect (e.g., browser refresh) only cancels the caller's await — the
        # task keeps running and persists the assistant message normally.
        task = asyncio.create_task(
            self._run_and_finalize(
                session=session,
                text=model_text,
                attempt_id=attempt_id,
                run_id=run_id,
                user_message=user_message,
                streaming_controller=streaming_controller,
                source_attribution=source_attribution,
                turn_context_reminder=turn_context_reminder,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._on_run_task_done)
        return await asyncio.shield(task)

    def _on_run_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        # Retrieve the exception so asyncio doesn't emit "Task exception was never
        # retrieved" when the caller awaiting asyncio.shield(task) was cancelled.
        if not task.cancelled():
            with contextlib.suppress(BaseException):
                task.result()

    async def _run_and_finalize(
        self,
        *,
        session: dict[str, Any],
        text: str,
        attempt_id: str,
        run_id: str,
        user_message: dict[str, Any],
        streaming_controller: Any = None,
        source_attribution: Mapping[str, str | None] | None = None,
        turn_context_reminder: str | None = None,
    ) -> dict[str, Any]:
        session_id = session["session_id"]
        try:
            try:
                answer, metadata = await self._run_loop(
                    session, text, attempt_id=attempt_id, run_id=run_id,
                    streaming_controller=streaming_controller,
                    source_attribution=source_attribution,
                    turn_context_reminder=turn_context_reminder,
                )
            except AssistantStoppedError as stopped:
                logger.info("assistant stopped by user session_id=%s attempt_id=%s", session_id, attempt_id)
                partial_text = str(stopped.partial_text or "")
                stopped_metadata = {
                    **stopped.metadata,
                    "stopped": True,
                    "partial": bool(partial_text),
                }
                partial_message = None
                if partial_text:
                    partial_message = await self.repository.append_message(
                        session_id=session_id,
                        role="assistant",
                        content=partial_text,
                        linked_attempt_id=attempt_id,
                        metadata=stopped_metadata,
                    )
                await self.repository.update_session(session_id, status="idle")
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="attempt.stopped",
                    payload={
                        "attempt_id": attempt_id,
                        "run_id": run_id,
                        "partial_content": partial_text,
                        "partial_message_id": partial_message["message_id"] if partial_message is not None else None,
                        **stopped.trace_payload,
                    },
                )
                raise ValueError("Assistant stopped by user") from None
            except Exception as exc:
                logger.exception("assistant run failed session_id=%s attempt_id=%s", session_id, attempt_id)
                await self.repository.update_session(session_id, status="error")
                run_error = exc if isinstance(exc, _AssistantRunError) else None
                trace_info = run_error.trace_payload if run_error else {}
                # The exception type the operator/user actually needs (e.g. ReadTimeout)
                # is the wrapped cause, not the _AssistantRunError wrapper.
                cause = getattr(exc, "__cause__", None) or exc
                error_type = type(cause).__name__
                partial_text = run_error.partial_text if run_error else ""
                # Persist a visible assistant message for the failure (mirrors the
                # user-stop path) so the chat reflects the failure instead of showing
                # only the user query. Always persist — even with no partial_text — so
                # a pre-stream failure (e.g. ReadTimeout before the first token) is
                # still surfaced rather than silently swallowed (CLAUDE.md §错误可见性).
                error_notice = partial_text or (
                    f"⚠️ 本轮运行失败（{error_type}）：{str(exc) or error_type}。"
                    "诊断详情见调试会话（trace 已记录）。"
                )
                failure_metadata: dict[str, Any] = {
                    "failed": True,
                    "partial": bool(partial_text),
                    "error": str(exc) or error_type,
                    "error_type": error_type,
                    "tool_calls": run_error.tool_events if run_error else [],
                    "thinking_blocks": run_error.thinking_blocks if run_error else [],
                    "content_blocks": run_error.content_blocks if run_error else [],
                    "trace": trace_info,
                }
                failed_message_id: str | None = None
                try:
                    failed_message = await self.repository.append_message(
                        session_id=session_id,
                        role="assistant",
                        content=error_notice,
                        linked_attempt_id=attempt_id,
                        metadata=failure_metadata,
                    )
                    failed_message_id = failed_message["message_id"]
                except Exception:
                    # Never let persistence of the failure notice mask the real error.
                    logger.exception(
                        "failed to persist assistant error message session_id=%s attempt_id=%s",
                        session_id,
                        attempt_id,
                    )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="attempt.failed",
                    payload={
                        "attempt_id": attempt_id,
                        "run_id": run_id,
                        "error": str(exc),
                        "error_type": error_type,
                        "message_id": failed_message_id,
                        **trace_info,
                    },
                )
                raise
        finally:
            self._clear_abort_event(session_id)

        assistant_message = await self.repository.append_message(
            session_id=session_id,
            role="assistant",
            content=answer,
            linked_attempt_id=attempt_id,
            metadata={**metadata, "thinking": metadata.get("thinking", "")},
        )
        session = await self.repository.update_session(session_id, status="idle")
        await self.repository.append_event(
            session_id=session_id,
            event_type="attempt.completed",
            payload={
                "attempt_id": attempt_id,
                "run_id": run_id,
                "message_id": assistant_message["message_id"],
                "summary": answer[:1000],
                **(metadata.get("trace") or {}),
            },
        )
        trace_id = (metadata.get("trace") or {}).get("trace_id")

        return {"session": session, "messages": [user_message, assistant_message], "trace_id": trace_id}

    async def _handle_lifecycle_command(self, session: dict[str, Any], command: str) -> dict[str, Any]:
        session_id = session["session_id"]
        await self.repository.append_event(
            session_id=session_id,
            event_type="lifecycle.command.received",
            payload={"command": command, "session_id": session_id},
        )
        if command == "new":
            new_session = await self.create_session(
                agent_id=session["agent_id"],
                title="",
            )
            await self.repository.append_event(
                session_id=session_id,
                event_type="session.superseded",
                payload={
                    "command": command,
                    "previous_session_id": session_id,
                    "new_session_id": new_session["session_id"],
                },
            )
            new_title = new_session.get("title") or "(无标题)"
            new_session_id = new_session["session_id"]
            # 构造 LifecycleReply 数据（供 channel.send_reply 渲染）
            reply = {
                "type": "lifecycle_notification",
                "title": "新会话已创建",
                "content": [
                    {"label": "标题", "value": new_title},
                    {"label": "会话 ID", "value": new_session_id[:8]},
                ],
                "footer": "会话已切换，请开始新对话",
            }
            return {
                "session": new_session,
                "messages": [],
                "trace_id": None,
                "lifecycle_command": {
                    "command": command,
                    "previous_session_id": session_id,
                    "new_session_id": new_session_id,
                },
                "reply": reply,
            }
        raise ValueError(f"unsupported lifecycle command: {command}")

    async def _handle_compact_command(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = session["session_id"]
        agent = None
        if self.agent_repo is not None:
            agent = await self.agent_repo.get_agent(session.get("agent_id"))
            if agent is None:
                raise ValueError(f"Agent not found: {session.get('agent_id')}")
        compaction_config = normalize_context_compaction_config(
            agent.get("context_compaction") if agent else None
        )
        if not compaction_config["enabled"] or not compaction_config["allow_slash_compact"]:
            raise ValueError("Context compaction is disabled for this agent")
        if self.model_adapter_factory is None:
            raise ValueError("Context compaction requires a model adapter")

        history_rows = await self.repository.list_messages(session_id, limit=100, offset=0)
        plan = build_full_compaction_plan(
            history_rows=history_rows,
            preserve_recent_messages=int(compaction_config["preserve_recent_messages"]),
            preserve_recent_tool_pairs=int(compaction_config["preserve_recent_tool_pairs"]),
        )
        if not plan.compacted_rows:
            raise ValueError("No earlier context is available to compact")

        model_route_name = (agent.get("model_route_name") if agent else None) or ""
        summary_model_route_name = (
            str(compaction_config.get("summary_model_route_name") or "").strip()
            or model_route_name
        )
        adapter = await self.model_adapter_factory(summary_model_route_name)
        attempt_id = f"compact-{uuid4().hex[:12]}"
        run_id = f"asst-compact-{uuid4().hex[:12]}"

        with debug_span_export_for_session(session_id, span_source="assistant"):
            with tracer.start_as_current_span("assistant.compaction") as span:
                span.set_attribute("assistant.session_id", session_id)
                span.set_attribute("assistant.attempt_id", attempt_id)
                span.set_attribute("doyoutrade.run_id", run_id)
                span.set_attribute("assistant.compaction.manual", True)
                span.set_attribute("assistant.compaction.source_message_count", len(plan.compacted_rows))
                trace_payload = _current_span_payload()
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="context_compaction.started",
                    payload={
                        "attempt_id": attempt_id,
                        "run_id": run_id,
                        "mode": "manual",
                        "source_message_count": len(plan.compacted_rows),
                        "summary_model_route_name": summary_model_route_name,
                        **trace_payload,
                    },
                )
                cycle_state = _AssistantCycleState(
                    task_id=None,
                    run_id=run_id,
                    trace_id=trace_payload.get("trace_id"),
                )
                summary_messages = build_summary_generation_messages(
                    compacted_rows=plan.compacted_rows,
                    latest_boundary_row=plan.latest_boundary_row,
                )
                with model_invocation_scope(
                    cycle_state,
                    "assistant_compaction",
                    extras={
                        "assistant_session_id": session_id,
                        "model_route_name": summary_model_route_name,
                    },
                ):
                    summary_text = await generate_compaction_summary(
                        adapter=adapter,
                        summary_messages=summary_messages,
                    )
                boundary_payload = dict(plan.boundary_metadata or {})
                summary_row = await self.append_summary_boundary_message(
                    session_id,
                    content=summary_text,
                    linked_attempt_id=attempt_id,
                    compacted_until_message_id=boundary_payload["context_compaction"]["compacted_until_message_id"],
                    source_message_count=boundary_payload["context_compaction"]["source_message_count"],
                )
                await self.record_compaction_state(
                    session_id,
                    summary_message_id=summary_row["message_id"],
                    compacted_until_message_id=boundary_payload["context_compaction"]["compacted_until_message_id"],
                    raw_message_count_at_compaction=boundary_payload["context_compaction"]["source_message_count"],
                )
                await self.repository.append_event(
                    session_id=session_id,
                    event_type="context_compaction.completed",
                    payload={
                        "attempt_id": attempt_id,
                        "run_id": run_id,
                        "mode": "manual",
                        "summary_message_id": summary_row["message_id"],
                        "compacted_until_message_id": boundary_payload["context_compaction"][
                            "compacted_until_message_id"
                        ],
                        "source_message_count": boundary_payload["context_compaction"]["source_message_count"],
                        "summary_model_route_name": summary_model_route_name,
                        **_current_span_payload(),
                    },
                )
                session = await self.repository.update_session(session_id, status="idle")
                return {
                    "session": session,
                    "messages": [summary_row],
                    "trace_id": trace_payload.get("trace_id"),
                }

    async def _run_loop(
        self,
        session: dict[str, Any],
        user_text: str,
        *,
        attempt_id: str,
        run_id: str,
        streaming_controller: Any = None,
        source_attribution: Mapping[str, str | None] | None = None,
        turn_context_reminder: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        trace_payload: dict[str, str | None] = {}
        tool_events: list[dict[str, Any]] = []
        # Hoisted before the ``try`` so the function-level ``except`` can ferry the
        # partial assistant output into ``_AssistantRunError`` on a mid-run failure.
        # A failure before the first stream delta (e.g. a ReadTimeout during the
        # compaction summary call) would otherwise hit the handler with these names
        # unbound and re-raise a NameError that buries the real exception.
        final_text = ""
        streamed_text = ""
        thinking_blocks: list[dict[str, Any]] = []
        content_blocks: list[dict[str, Any]] = []
        stream_batcher = (
            _StreamingControllerBatcher(streaming_controller)
            if streaming_controller is not None
            else None
        )
        abort_event = self._get_abort_event(session["session_id"])
        try:
            if abort_event.is_set():
                raise AssistantStoppedError()
            with debug_span_export_for_session(session["session_id"], span_source="assistant"):
                with tracer.start_as_current_span("assistant.loop") as span:
                    span.set_attribute("assistant.session_id", session["session_id"])
                    span.set_attribute("assistant.attempt_id", attempt_id)
                    span.set_attribute("doyoutrade.run_id", run_id)
                    trace_payload = _current_span_payload()
                    await self.repository.append_event(
                        session_id=session["session_id"],
                        event_type="attempt.started",
                        payload={"attempt_id": attempt_id, "run_id": run_id, **trace_payload},
                    )
                    agent = None
                    if self.agent_repo is not None:
                        agent = await self.agent_repo.get_agent(session.get("agent_id"))
                        if agent is None:
                            raise ValueError(f"Agent not found: {session.get('agent_id')}")

                    resolved_tools = self._resolve_tool_inventory(session, agent)
                    session_config = dict(session.get("config") or {})
                    system_prompt = str(session_config.get("system_prompt_snapshot") or "").strip()
                    if not system_prompt:
                        system_prompt = _compose_effective_system_prompt(
                            resolved_tools.registry,
                            agent,
                        )
                    history_rows = await self.repository.list_messages(
                        session["session_id"],
                        limit=100,
                        offset=0,
                    )
                    history_messages = _conversation_messages_from_rows(
                        history_rows, user_text
                    )
                    history_messages = await _inject_loaded_skills_reminder(
                        history_messages,
                        session_id=session["session_id"],
                        history_rows=history_rows,
                        loaded_skill_repository=self._loaded_skill_repository,
                    )
                    history_messages = await self._inject_active_flow_reminder(
                        session,
                        history_messages,
                        attempt_id=attempt_id,
                        run_id=run_id,
                    )
                    history_messages = _inject_turn_context_reminder(
                        history_messages,
                        turn_context_reminder,
                    )
                    history_messages = _inject_runtime_context_reminder(history_messages)
                    compaction_config = normalize_context_compaction_config(
                        agent.get("context_compaction") if agent else None
                    )
                    prepared_context = prepare_messages_for_model(
                        system_prompt=system_prompt,
                        history_messages=history_messages,
                        config=compaction_config,
                    )
                    messages: list[Any] = list(prepared_context.messages)
                    if self.model_adapter_factory is None:
                        fallback = self._fallback_answer(user_text)
                        if stream_batcher is not None:
                            await stream_batcher.publish("text", fallback)
                            await stream_batcher.flush_all()
                        return fallback, {"tool_calls": [], "trace": trace_payload}

                    model_route_name = (agent.get("model_route_name") if agent else None) or ""
                    adapter = await self.model_adapter_factory(model_route_name)
                    # Source attribution: when this turn is a compose-only step
                    # owned by an external flow (e.g. a trigger's prose push),
                    # attribute the model invocation to THAT flow's run_id/task_id
                    # so it shows up under the originating cycle's 周期详情 model
                    # invocations — not an opaque asst-run id (CLAUDE.md run_id 贯穿).
                    # The assistant run's own span/run_id (above) stays unchanged.
                    attrib = source_attribution or {}
                    cycle_state = _AssistantCycleState(
                        task_id=attrib.get("task_id"),
                        run_id=attrib.get("run_id") or run_id,
                        trace_id=trace_payload.get("trace_id"),
                    )
                    full_compaction_metadata: dict[str, Any] = {}
                    prepared_context = prepare_messages_for_model(
                        system_prompt=system_prompt,
                        history_messages=history_messages,
                        config=compaction_config,
                    )
                    compaction_decision = evaluate_full_compaction(
                        estimated_tokens=prepared_context.estimated_tokens,
                        config=compaction_config,
                    )
                    if compaction_decision.should_compact:
                        plan = build_full_compaction_plan(
                            history_rows=history_rows,
                            preserve_recent_messages=int(compaction_config["preserve_recent_messages"]),
                            preserve_recent_tool_pairs=int(compaction_config["preserve_recent_tool_pairs"]),
                        )
                        if plan.compacted_rows:
                            summary_model_route_name = (
                                str(compaction_config.get("summary_model_route_name") or "").strip()
                                or model_route_name
                            )
                            summary_adapter = adapter
                            if summary_model_route_name != model_route_name:
                                summary_adapter = await self.model_adapter_factory(summary_model_route_name)
                            summary_messages = build_summary_generation_messages(
                                compacted_rows=plan.compacted_rows,
                                latest_boundary_row=plan.latest_boundary_row,
                            )
                            span.set_attribute("assistant.compaction.auto_triggered", True)
                            span.set_attribute("assistant.compaction.source_message_count", len(plan.compacted_rows))
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="context_compaction.started",
                                payload={
                                    "attempt_id": attempt_id,
                                    "run_id": run_id,
                                    "mode": "auto",
                                    "threshold_tokens": compaction_decision.threshold_tokens,
                                    "estimated_tokens": prepared_context.estimated_tokens,
                                    "source_message_count": len(plan.compacted_rows),
                                    "summary_model_route_name": summary_model_route_name,
                                    **_current_span_payload(),
                                },
                            )
                            with model_invocation_scope(
                                cycle_state,
                                "assistant_compaction",
                                extras={
                                    "assistant_session_id": session["session_id"],
                                    "model_route_name": summary_model_route_name,
                                },
                            ):
                                summary_text = await generate_compaction_summary(
                                    adapter=summary_adapter,
                                    summary_messages=summary_messages,
                                )
                            boundary_payload = dict(plan.boundary_metadata or {})
                            summary_row = await self.append_summary_boundary_message(
                                session["session_id"],
                                content=summary_text,
                                linked_attempt_id=attempt_id,
                                compacted_until_message_id=boundary_payload["context_compaction"][
                                    "compacted_until_message_id"
                                ],
                                source_message_count=boundary_payload["context_compaction"]["source_message_count"],
                            )
                            await self.record_compaction_state(
                                session["session_id"],
                                summary_message_id=summary_row["message_id"],
                                compacted_until_message_id=boundary_payload["context_compaction"][
                                    "compacted_until_message_id"
                                ],
                                raw_message_count_at_compaction=boundary_payload["context_compaction"][
                                    "source_message_count"
                                ],
                            )
                            history_rows = [summary_row, *plan.tail_rows]
                            history_messages = _conversation_messages_from_rows(
                                history_rows, user_text
                            )
                            history_messages = await _inject_loaded_skills_reminder(
                                history_messages,
                                session_id=session["session_id"],
                                history_rows=history_rows,
                                loaded_skill_repository=self._loaded_skill_repository,
                            )
                            history_messages = _inject_turn_context_reminder(
                                history_messages,
                                turn_context_reminder,
                            )
                            history_messages = _inject_runtime_context_reminder(history_messages)
                            prepared_context = prepare_messages_for_model(
                                system_prompt=system_prompt,
                                history_messages=history_messages,
                                config=compaction_config,
                            )
                            full_compaction_metadata = {
                                "context_compaction": {
                                    "full_applied": True,
                                    "summary_message_id": summary_row["message_id"],
                                    "compacted_until_message_id": boundary_payload["context_compaction"][
                                        "compacted_until_message_id"
                                    ],
                                    "summary_model_route_name": summary_model_route_name,
                                }
                            }
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="context_compaction.completed",
                                payload={
                                    "attempt_id": attempt_id,
                                    "run_id": run_id,
                                    "mode": "auto",
                                    "threshold_tokens": compaction_decision.threshold_tokens,
                                    "estimated_tokens": prepared_context.estimated_tokens,
                                    "summary_message_id": summary_row["message_id"],
                                    "compacted_until_message_id": boundary_payload["context_compaction"][
                                        "compacted_until_message_id"
                                    ],
                                    "source_message_count": boundary_payload["context_compaction"][
                                        "source_message_count"
                                    ],
                                    "summary_model_route_name": summary_model_route_name,
                                    **_current_span_payload(),
                                },
                            )
                    # (final_text/streamed_text/thinking_blocks/content_blocks are
                    # initialized before the ``try`` so the failure handler can read
                    # them; they are not re-declared here.)

                    def _stopped_error() -> AssistantStoppedError:
                        partial_text = streamed_text or final_text
                        stopped_content_blocks = _content_blocks_with_partial_text(
                            content_blocks, partial_text
                        )
                        return AssistantStoppedError(
                            partial_text=partial_text,
                            trace_payload=trace_payload,
                            metadata={
                                "tool_calls": tool_events,
                                "trace": trace_payload,
                                **full_compaction_metadata,
                                "thinking": thinking_blocks[-1]["content"] if thinking_blocks else "",
                                "thinking_blocks": thinking_blocks,
                                "content_blocks": stopped_content_blocks,
                            },
                        )

                    for turn in range(agent.get("max_turns", self.max_turns) if agent else self.max_turns):
                        current_session = await self.repository.get_session(session["session_id"])
                        if current_session is not None:
                            session = current_session
                        resolved_tools = self._resolve_tool_inventory(session, agent)
                        tool_definitions = self._tool_definitions_for_model(resolved_tools)
                        await self.repository.append_event(
                            session_id=session["session_id"],
                            event_type="tool_inventory.resolved",
                            payload={
                                "attempt_id": attempt_id,
                                "run_id": run_id,
                                "turn": turn,
                                "base_tool_names": resolved_tools.base_tool_names,
                                "deferred_tool_names": resolved_tools.deferred_tool_names,
                                "activated_deferred_tool_names": resolved_tools.activated_deferred_tool_names,
                                "effective_tool_names": resolved_tools.effective_tool_names,
                                **_current_span_payload(),
                            },
                        )
                        turn_thinking = ""
                        # Reset per turn so each contiguous run of streamed text is
                        # published as its own segment. A cumulative-across-turns
                        # ``streamed_text`` would make the streaming card controller
                        # merge every turn's text (prefaces + final answer) into one
                        # card, losing the real interleaving with tool calls.
                        streamed_text = ""
                        # Reset per turn too: a failure on this turn's model call must
                        # not attribute a *prior* turn's final_text as this turn's
                        # partial output. Without this, a pre-stream failure on turn N>0
                        # would persist turn N-1's text as "partial" and suppress the
                        # real error notice (partial_text = streamed_text or final_text).
                        final_text = ""
                        if turn > 0:
                            prepared_context = prepare_messages_for_model(
                                system_prompt=system_prompt,
                                history_messages=messages[1:],
                                config=compaction_config,
                            )
                        messages = list(prepared_context.messages)

                        async def _on_text_delta(delta: str) -> None:
                            nonlocal streamed_text
                            if not delta:
                                return
                            streamed_text += str(delta)
                            if stream_batcher is not None:
                                await stream_batcher.publish("text", streamed_text)
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="message.delta",
                                payload={
                                    "attempt_id": attempt_id,
                                    "run_id": run_id,
                                    "turn": turn,
                                    "delta": str(delta),
                                    "content": streamed_text,
                                    **_current_span_payload(),
                                },
                            )

                        async def _on_thinking_delta(delta: str) -> None:
                            nonlocal turn_thinking
                            if not delta:
                                return
                            turn_thinking += str(delta)
                            if stream_batcher is not None:
                                await stream_batcher.publish("thinking", turn_thinking)
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="thinking.delta",
                                payload={
                                    "attempt_id": attempt_id,
                                    "run_id": run_id,
                                    "turn": turn,
                                    "delta": str(delta),
                                    **_current_span_payload(),
                                },
                            )

                        if abort_event.is_set():
                            raise _stopped_error()

                        async def _await_with_abort(coro: Awaitable[Any]) -> Any:
                            """Race *coro* against the session abort event (OpenClaw chat.abort pattern)."""
                            if abort_event.is_set():
                                raise _stopped_error()
                            task = asyncio.ensure_future(coro)
                            abort_wait_task = asyncio.ensure_future(abort_event.wait())
                            try:
                                done, _pending = await asyncio.wait(
                                    {task, abort_wait_task},
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                            finally:
                                if not abort_wait_task.done():
                                    abort_wait_task.cancel()
                                    with contextlib.suppress(BaseException):
                                        await abort_wait_task
                            if abort_event.is_set() and task not in done:
                                task.cancel()
                                with contextlib.suppress(BaseException):
                                    await task
                                logger.info(
                                    "assistant model call stopped by user session_id=%s attempt_id=%s turn=%s",
                                    session["session_id"],
                                    attempt_id,
                                    turn,
                                )
                                raise _stopped_error()
                            return task.result()

                        # Retry the model call (only) on a transient transport error
                        # before any tool is dispatched. The tool loop is below, so a
                        # retry here cannot re-execute non-idempotent tools. A single
                        # streaming ReadTimeout previously killed the whole turn.
                        response = None
                        for model_attempt in range(self._model_transport_max_retries + 1):
                            try:
                                with tracer.start_as_current_span("assistant.model") as model_span:
                                    model_span.set_attribute("assistant.turn", turn)
                                    model_span.set_attribute("doyoutrade.run_id", run_id)
                                    model_span.set_attribute("assistant.session_id", session["session_id"])
                                    model_span.set_attribute(
                                        "assistant.context.estimated_tokens",
                                        prepared_context.estimated_tokens,
                                    )
                                    model_span.set_attribute(
                                        "assistant.context.micro_compaction_applied",
                                        prepared_context.micro_compaction_applied,
                                    )
                                    model_span.set_attribute(
                                        "assistant.context.full_compaction_applied",
                                        bool(full_compaction_metadata),
                                    )
                                    if model_attempt > 0:
                                        model_span.set_attribute(
                                            "assistant.model.transport_retry", model_attempt
                                        )
                                    with model_invocation_scope(
                                        cycle_state,
                                        "assistant_loop",
                                        extras={
                                            "assistant_session_id": session["session_id"],
                                            "model_route_name": model_route_name,
                                            "assistant_base_tool_names": resolved_tools.base_tool_names,
                                            "assistant_deferred_tool_names": resolved_tools.deferred_tool_names,
                                            "assistant_effective_tool_names": resolved_tools.effective_tool_names,
                                        },
                                    ):
                                        agent_turn = getattr(adapter, "agent_turn", None)
                                        if agent_turn is not None:
                                            response = await _await_with_abort(
                                                agent_turn(
                                                    messages,
                                                    tools=tool_definitions,
                                                    on_text_delta=_on_text_delta,
                                                    on_thinking_delta=_on_thinking_delta,
                                                )
                                            )
                                        else:
                                            chat_ainvoke = getattr(adapter, "chat_ainvoke", None)
                                            if chat_ainvoke is not None:
                                                response = await _await_with_abort(
                                                    chat_ainvoke(messages, tools=tool_definitions)
                                                )
                                            else:
                                                response = await _await_with_abort(
                                                    asyncio.to_thread(adapter.generate, None)
                                                )  # pragma: no cover - legacy adapter escape hatch
                                break
                            except AssistantStoppedError:
                                raise
                            except Exception as model_exc:
                                if (
                                    model_attempt >= self._model_transport_max_retries
                                    or abort_event.is_set()
                                    or not _is_retryable_model_transport_error(model_exc)
                                ):
                                    raise
                                logger.warning(
                                    "assistant model call transport error; retrying "
                                    "session_id=%s attempt_id=%s turn=%s model_attempt=%s "
                                    "error_type=%s error=%s",
                                    session["session_id"],
                                    attempt_id,
                                    turn,
                                    model_attempt + 1,
                                    type(model_exc).__name__,
                                    model_exc,
                                )
                                await self.repository.append_event(
                                    session_id=session["session_id"],
                                    event_type="assistant_model_transport_retry",
                                    payload={
                                        "attempt_id": attempt_id,
                                        "run_id": run_id,
                                        "turn": turn,
                                        "model_attempt": model_attempt + 1,
                                        "max_retries": self._model_transport_max_retries,
                                        "reason": "model_transport_timeout",
                                        "error_type": type(model_exc).__name__,
                                        "error": str(model_exc),
                                        "hint": (
                                            "raise model_route timeout_seconds or lower "
                                            "max_tokens if this recurs"
                                        ),
                                        **_current_span_payload(),
                                    },
                                )
                                # Reset the per-turn stream accumulators so the retried
                                # stream republishes cleanly instead of doubling the
                                # partial text already emitted on this failed attempt.
                                streamed_text = ""
                                turn_thinking = ""
                        if isinstance(response, AgentTurnResponse):
                            turn_response = response
                        else:
                            turn_response = agent_turn_response_from_model_response(response)
                        raw = getattr(turn_response, "raw", None)
                        tool_calls = list(turn_response.tool_calls)
                        if not tool_calls:
                            tool_calls = list(getattr(raw, "tool_calls", None) or [])
                        final_text = _message_content_text(turn_response.content or getattr(raw, "content", ""))
                        message_tool_calls = [_tool_call_for_message(tc) for tc in tool_calls]
                        messages.append(AIMessage(content=final_text, tool_calls=message_tool_calls))

                        # Emit thinking_done after each model's turn completes
                        if turn_thinking:
                            thinking_blocks.append({"turn": turn, "content": turn_thinking})
                            content_blocks.append({"type": "thinking", "turn": turn, "content": turn_thinking})
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="thinking.done",
                                payload={
                                    "attempt_id": attempt_id,
                                    "run_id": run_id,
                                    "turn": turn,
                                    "thinking": turn_thinking,
                                    **_current_span_payload(),
                                },
                            )

                        if final_text and tool_calls:
                            content_blocks.append({"type": "text", "turn": turn, "content": final_text})

                        if stream_batcher is not None:
                            await stream_batcher.flush_all()
                        if not tool_calls:
                            # Flow advancement consumes any trailing <choice>
                            # tag and returns the text stripped of flow-control
                            # markup; the raw text is kept for the streamed-
                            # suffix comparison below (the stream already
                            # carried the tag).
                            raw_final_text = final_text
                            final_text = await self._maybe_advance_flow(
                                session["session_id"],
                                final_text,
                                attempt_id=attempt_id,
                                run_id=run_id,
                                span=span,
                            )
                            # Republish final_text only as a fallback for adapters
                            # that don't stream it via on_text_delta. When the turn
                            # already streamed it, streamed_text (cumulative across
                            # turns) ends with final_text; republishing the shorter
                            # per-turn final_text would trip the card controller's
                            # "shorter = new reply" boundary heuristic and duplicate
                            # the answer inside the card.
                            if (
                                stream_batcher is not None
                                and raw_final_text
                                and not streamed_text.endswith(raw_final_text)
                            ):
                                await stream_batcher.publish("text", final_text)
                                await stream_batcher.flush_all()
                            if final_text:
                                content_blocks.append({"type": "text", "content": final_text})
                            return final_text or "已完成。", {
                                "tool_calls": tool_events,
                                "trace": trace_payload,
                                **full_compaction_metadata,
                                "thinking": thinking_blocks[-1]["content"] if thinking_blocks else "",
                                "thinking_blocks": thinking_blocks,
                                "content_blocks": content_blocks,
                            }

                        for tool_call in tool_calls:
                            if stream_batcher is not None:
                                await stream_batcher.flush_all()
                            name, call_id, args = _tool_call_parts(tool_call)
                            tool = resolved_tools.registry.get(name)
                            tool_category = "agent" if name == "discover_tools" else (getattr(tool, "category", None) if tool is not None else None)
                            tool_block = {
                                "type": "tool_call",
                                "tool_call_id": call_id,
                                "name": name,
                                "arguments": args,
                                "category": tool_category,
                                "status": "running",
                            }
                            content_blocks.append(tool_block)
                            event_payload = {
                                "attempt_id": attempt_id,
                                "run_id": run_id,
                                "tool": name,
                                "tool_call_id": call_id,
                                "arguments": args,
                            }
                            if abort_event.is_set():
                                raise _stopped_error()
                            with tracer.start_as_current_span("assistant.tool") as tool_span:
                                tool_span.set_attribute("assistant.tool.name", name)
                                tool_span.set_attribute("assistant.session_id", session["session_id"])
                                tool_span.set_attribute("doyoutrade.run_id", run_id)
                                await self.repository.append_event(
                                    session_id=session["session_id"],
                                    event_type="tool.call",
                                    payload={**event_payload, **_current_span_payload()},
                                )
                                if streaming_controller is not None:
                                    on_tool_start = getattr(streaming_controller, "on_tool_start", None)
                                    if on_tool_start is not None:
                                        await on_tool_start(
                                            name,
                                            tool_call_id=call_id,
                                            arguments=args,
                                            category=tool_category,
                                        )
                                if name == "discover_tools":
                                    tool_coro = self._execute_discover_tools(
                                        session_id=session["session_id"],
                                        args=args,
                                        resolved=resolved_tools,
                                        attempt_id=attempt_id,
                                        run_id=run_id,
                                    )
                                else:
                                    # Approval gate runs inside the execution
                                    # slot so the abort race below also covers
                                    # the human-wait period.
                                    async def _gated_execute(
                                        _name: str = name, _args: dict[str, Any] = args
                                    ) -> Any:
                                        blocked = await self._gate_tool_call_for_approval(
                                            session_id=session["session_id"],
                                            tool_name=_name,
                                            arguments=_args,
                                            attempt_id=attempt_id,
                                            run_id=run_id,
                                            streaming_controller=streaming_controller,
                                        )
                                        if blocked is not None:
                                            return blocked
                                        tool_result = await resolved_tools.registry.execute(
                                            _name,
                                            _args,
                                            session_id=session["session_id"],
                                            calling_agent_id=session.get("agent_id"),
                                        )
                                        # ask_user_question blocks (fizz-style):
                                        # publish the card, then suspend for the
                                        # user's answer INSIDE this slot so the
                                        # abort race covers the human-wait. The
                                        # answer replaces the tool result and is
                                        # fed back as this call's tool_result —
                                        # no synthetic user message, same run.
                                        if (
                                            _name == "ask_user_question"
                                            and not _tool_result_is_error(tool_result)
                                        ):
                                            return await self._await_user_question_answer(
                                                session["session_id"],
                                                attempt_id=attempt_id,
                                                run_id=run_id,
                                                content_blocks=content_blocks,
                                                streaming_controller=streaming_controller,
                                            )
                                        return tool_result

                                    tool_coro = _gated_execute()
                                # Race the tool against the abort event so that a
                                # user-initiated stop is observed *during* tool
                                # execution, not only between tools. Without this,
                                # long-running tools (backtests, batch jobs) ignore
                                # the stop button until they finish on their own.
                                tool_task = asyncio.ensure_future(tool_coro)
                                abort_wait_task = asyncio.ensure_future(abort_event.wait())
                                try:
                                    done, _pending = await asyncio.wait(
                                        {tool_task, abort_wait_task},
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )
                                finally:
                                    if not abort_wait_task.done():
                                        abort_wait_task.cancel()
                                        with contextlib.suppress(BaseException):
                                            await abort_wait_task
                                if tool_task in done:
                                    result = tool_task.result()
                                else:
                                    tool_task.cancel()
                                    with contextlib.suppress(BaseException):
                                        await tool_task
                                    stopped_preview = (
                                        f'{{"status":"stopped","reason":"user_stop","tool":"{name}"}}'
                                    )
                                    tool_block.update(
                                        {
                                            "status": "stopped",
                                            "result_preview": stopped_preview,
                                            "is_error": True,
                                        }
                                    )
                                    tool_events.append(
                                        {**event_payload, "result_preview": stopped_preview, "stopped": True}
                                    )
                                    await self.repository.append_event(
                                        session_id=session["session_id"],
                                        event_type="tool.result",
                                        payload={
                                            **event_payload,
                                            "preview": stopped_preview,
                                            "stopped": True,
                                            **_current_span_payload(),
                                        },
                                    )
                                    if streaming_controller is not None:
                                        on_tool_result = getattr(
                                            streaming_controller, "on_tool_result", None
                                        )
                                        if on_tool_result is not None:
                                            await on_tool_result(
                                                call_id,
                                                name=name,
                                                preview=stopped_preview,
                                                is_error=True,
                                            )
                                    tool_span.set_status(
                                        Status(StatusCode.ERROR, "assistant tool stopped by user")
                                    )
                                    tool_span.set_attribute("assistant.tool.stopped", True)
                                    logger.info(
                                        "assistant tool stopped by user session_id=%s tool=%s call_id=%s",
                                        session["session_id"],
                                        name,
                                        call_id,
                                    )
                                    raise _stopped_error()
                                tool_span.set_status(Status(StatusCode.OK))
                            # Tools that opt out of truncation (e.g. ``load_skill``)
                            # must surface their full payload in the persisted
                            # message so ``/assistant/sessions/{id}/messages``
                            # returns content identical to what the tool produced.
                            if getattr(tool, "bypass_result_truncation", False):
                                preview = result
                            else:
                                preview = result[:1000]
                            tool_events.append({**event_payload, "result_preview": preview})
                            tool_block.update(
                                {
                                    "status": "error" if _tool_result_is_error(result) else "completed",
                                    "result_preview": preview,
                                    "is_error": _tool_result_is_error(result),
                                }
                            )
                            await self.repository.append_event(
                                session_id=session["session_id"],
                                event_type="tool.result",
                                payload={**event_payload, "preview": preview, **_current_span_payload()},
                            )
                            if streaming_controller is not None:
                                on_tool_result = getattr(streaming_controller, "on_tool_result", None)
                                if on_tool_result is not None:
                                    await on_tool_result(
                                        call_id,
                                        name=name,
                                        preview=preview,
                                        is_error=_tool_result_is_error(result),
                                    )
                            # ask_user_question publishes its card and blocks
                            # for the answer inside _gated_execute above; the
                            # resolved answer is already in ``result`` here and
                            # feeds back as this call's tool_result.
                            messages.append(
                                ToolMessage(content=result, tool_call_id=call_id, name=name)
                            )
                    if stream_batcher is not None:
                        await stream_batcher.flush_all()
                    configured_max_turns = (
                        agent.get("max_turns", self.max_turns) if agent else self.max_turns
                    )
                    span.set_status(Status(StatusCode.ERROR, "assistant loop reached max turns"))
                    span.set_attribute("assistant.max_turns_reached", True)
                    span.set_attribute("assistant.max_turns", configured_max_turns)
                    await self.repository.append_event(
                        session_id=session["session_id"],
                        event_type="attempt.max_turns_reached",
                        payload={
                            "attempt_id": attempt_id,
                            "run_id": run_id,
                            "max_turns": configured_max_turns,
                            "tool_call_count": len(tool_events),
                            "hint": (
                                "raise agent.max_turns or narrow the request scope; "
                                "the model was still issuing tool_calls when the turn "
                                "budget ran out"
                            ),
                            **_current_span_payload(),
                        },
                    )
                    # ``final_text`` here is only the last turn's pre-tool-call preface
                    # (already recorded as its own block by the "final_text and
                    # tool_calls" branch above) — it is never the intended cutoff
                    # notice. Reusing it via ``final_text or notice`` meant the notice
                    # almost never fired (models routinely narrate before calling
                    # tools), so the chat silently stopped at the last tool call with
                    # no visible sign the turn budget was exhausted. Always surface a
                    # distinct notice instead (CLAUDE.md §错误可见性).
                    max_turns_notice = "工具调用轮次已达上限，请缩小问题范围后继续。"
                    content_blocks.append({"type": "text", "content": max_turns_notice})
                    if stream_batcher is not None:
                        await stream_batcher.publish("text", max_turns_notice)
                        await stream_batcher.flush_all()
                    return max_turns_notice, {
                        "tool_calls": tool_events,
                        "trace": trace_payload,
                        "max_turns_reached": True,
                        **full_compaction_metadata,
                        "thinking": thinking_blocks[-1]["content"] if thinking_blocks else "",
                        "thinking_blocks": thinking_blocks,
                        "content_blocks": content_blocks,
                    }
        except asyncio.CancelledError:
            raise AssistantStoppedError()
        except AssistantStoppedError:
            raise
        except Exception as exc:  # noqa: E722
            # Ferry the partial work out of the span context so _run_and_finalize can
            # persist a visible partial/error assistant message instead of leaving the
            # chat showing only the user's query. All accumulators are bound (hoisted
            # before the try), so a pre-stream failure still yields a clean payload.
            partial_text = streamed_text or final_text
            raise _AssistantRunError(
                trace_payload,
                exc,
                partial_text=partial_text,
                content_blocks=_content_blocks_with_partial_text(content_blocks, partial_text),
                tool_events=tool_events,
                thinking_blocks=thinking_blocks,
            ) from exc

    def _fallback_answer(self, user_text: str) -> str:
        return (
            "已创建 DoYouTrade Agent 会话。当前未配置可用模型路由，因此先给出执行建议："
            "可以继续提供策略规则、目标标的和回测区间；配置模型路由后 Agent 将能调用 "
            "`load_skill`、策略资源工具和 `run_strategy_backtest`。"
            f"\n\n用户需求：{user_text}"
        )

    async def _generate_title_background(
        self,
        session_id: str,
        first_message: str,
    ) -> None:
        """后台任务：为首条消息生成 session 标题。"""
        from doyoutrade.assistant.title_generator import generate_session_title

        try:
            session = await self.repository.get_session(session_id)
            if session is None:
                return
            if not _should_generate_session_title(session.get("title", "")):
                return

            agent = None
            if self.agent_repo is not None:
                agent = await self.agent_repo.get_agent(session.get("agent_id"))
            model_route_name = (agent.get("model_route_name") if agent else None) or ""

            title = await generate_session_title(
                first_message,
                model_route_name,
                self.model_adapter_factory,
            )
            if title:
                await self.repository.update_session(session_id, title=title)
        except Exception:
            # 静默降级，不影响主流程
            pass
