from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, cast, delete, func, not_, or_, select, String, update

from doyoutrade.persistence.errors import (
    AgentInUseError,
    BuiltinAgentImmutableError,
    RecordNotFoundError,
)
from doyoutrade.persistence.models import (
    AgentRecord,
    AssistantEventRecord,
    AssistantLoadedSkillRecord,
    AssistantMessageRecord,
    AssistantSessionRecord,
    ChannelPeerSessionRecord,
    ChannelRecord,
    DebugSessionSpanRecord,
    ModelInvocationRecord,
)
from doyoutrade.assistant.main_agent import (
    MAIN_AGENT_EDITABLE_FIELDS,
    MAIN_AGENT_ID,
    MAIN_AGENT_NAME,
    MAIN_AGENT_PROMPT_TEMPLATE_ID,
    apply_main_agent_overrides,
    builtin_agent_identity,
    builtin_skill_names,
    builtin_tool_names,
    is_builtin_agent,
    is_main_agent,
)
from doyoutrade.assistant.prompt_templates import resolve_agent_system_prompt
from doyoutrade.assistant.signal_composer_agent import (
    SIGNAL_COMPOSER_AGENT_ID,
    SIGNAL_COMPOSER_AGENT_NAME,
    SIGNAL_COMPOSER_EDITABLE_FIELDS,
    SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
)

logger = logging.getLogger(__name__)


DEFAULT_CONTEXT_COMPACTION: dict[str, Any] = {
    "enabled": True,
    "mode": "auto",
    "trigger_strategy": "token_estimate",
    "auto_threshold_tokens": 24000,
    "warning_threshold_tokens": 20000,
    "preserve_recent_messages": 12,
    "preserve_recent_tool_pairs": 4,
    "micro_compaction_enabled": True,
    "tool_result_max_chars": 4000,
    "full_compaction_enabled": True,
    "summary_model_route_name": "",
    "allow_slash_compact": True,
}


def normalize_tool_configs(
    tool_configs: list[dict[str, Any]] | None,
    *,
    fallback_tool_names: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()

    if isinstance(tool_configs, list):
        entries = tool_configs
    else:
        entries = [{"name": name, "load_mode": "base"} for name in list(fallback_tool_names or [])]

    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise ValueError(f"tool_configs[{index}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"tool_configs[{index}].name is required")
        load_mode = str(raw.get("load_mode") or "base").strip().lower()
        if load_mode not in {"base", "deferred"}:
            raise ValueError(f"tool_configs[{index}].load_mode must be one of: base, deferred")
        if name in seen:
            continue
        seen.add(name)
        normalized.append({"name": name, "load_mode": load_mode})
    return normalized


def derive_tool_names(tool_configs: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("name")) for item in list(tool_configs or []) if str(item.get("name") or "").strip()]


def normalize_context_compaction_config(value: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONTEXT_COMPACTION)
    if isinstance(value, dict):
        cfg.update({key: item for key, item in value.items() if item is not None})
    return cfg


def _copy_agent_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    copied["tool_configs"] = normalize_tool_configs(
        row.get("tool_configs"),
        fallback_tool_names=row.get("tool_names"),
    )
    copied["tool_names"] = derive_tool_names(copied["tool_configs"])
    copied["skill_names"] = list(row.get("skill_names") or [])
    copied["context_compaction"] = normalize_context_compaction_config(
        row.get("context_compaction")
    )
    template_id = str(
        row.get("prompt_template_id")
        or row.get("system_prompt_template_id")
        or ""
    ).strip() or None
    copied["system_prompt_template_id"] = template_id
    copied["prompt_template_id"] = template_id
    # Pin code-controlled identity (name / prompt template / flags) for the
    # builtin main agent BEFORE resolving the prompt, so resolution renders the
    # locked main_agent.j2 template.
    apply_main_agent_overrides(copied)
    copied["resolved_system_prompt"] = resolve_agent_system_prompt(copied)
    return copied


def _agent_is_builtin(record: Any) -> bool:
    """True if ``record`` is ANY code-fixed builtin agent (ORM record or dict).

    Covers both the main agent and the signal-card composer. Keys on the
    ``is_builtin`` flag (both set it) plus explicit id match for robustness
    before the migration has run."""
    if isinstance(record, dict):
        rid = record.get("id")
        return bool(record.get("is_builtin")) or is_builtin_agent(rid)
    rid = getattr(record, "id", None)
    return bool(getattr(record, "is_builtin", False)) or is_builtin_agent(rid)


def _reject_builtin_locked_updates(record: Any, updates: dict[str, Any]) -> None:
    """Block edits to locked fields of ANY builtin agent (visible refusal).

    Each builtin exposes its own editable surface
    (``MAIN_AGENT_EDITABLE_FIELDS`` / ``SIGNAL_COMPOSER_EDITABLE_FIELDS``);
    any other key present raises ``BuiltinAgentImmutableError`` instead of
    being silently dropped (CLAUDE.md §错误可见性)."""
    if not _agent_is_builtin(record):
        return
    rid = record.get("id") if isinstance(record, dict) else getattr(record, "id", None)
    identity = builtin_agent_identity(rid)
    editable = identity[3] if identity is not None else ()
    locked = [key for key in updates if key not in editable]
    if locked:
        raise BuiltinAgentImmutableError(
            f"agent {rid!r} is a code-fixed builtin agent; only "
            f"{', '.join(editable)} are editable "
            f"(rejected: {', '.join(sorted(locked))})"
        )


def _reject_builtin_delete(record: Any) -> None:
    """Block deletion of ANY builtin agent (visible refusal)."""
    if _agent_is_builtin(record):
        rid = record.get("id") if isinstance(record, dict) else getattr(record, "id", None)
        raise BuiltinAgentImmutableError(
            f"agent {rid!r} is a code-fixed builtin agent and cannot be deleted"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _session_dict(record: AssistantSessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "agent_id": record.agent_id,
        "title": record.title,
        "status": record.status,
        "config": dict(record.config or {}),
        "channel_source": _derive_channel_source(record.session_id, record.config or {}),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "last_attempt_id": record.last_attempt_id,
    }


def _config_channel_value(*path: str):
    """Dialect-portable extraction of a nested ``config`` JSON value as text.

    Compiles to ``json_extract`` on SQLite and the ``#>>`` path operator on
    PostgreSQL, avoiding the SQLite-only ``json_extract`` SQL function that
    breaks against asyncpg (UndefinedFunctionError: function json_extract...).
    """
    return cast(AssistantSessionRecord.config[path].as_string(), String)


def _is_channel_session_sql_predicate():
    config_channel_id = _config_channel_value("channel", "channel_id")
    config_channel_type = _config_channel_value("channel", "channel_type")
    return or_(
        and_(config_channel_id.isnot(None), config_channel_id != ""),
        and_(config_channel_type.isnot(None), config_channel_type != ""),
        AssistantSessionRecord.session_id.like("channel:%"),
    )


def _build_list_sessions_filters(
    *,
    channel_id: str | None,
    source: str | None,
) -> list[Any]:
    filters: list[Any] = []
    normalized_channel_id = str(channel_id or "").strip()
    normalized_source = str(source or "").strip().lower()

    if normalized_channel_id:
        config_channel_id = _config_channel_value("channel", "channel_id")
        filters.append(
            or_(
                config_channel_id == normalized_channel_id,
                AssistantSessionRecord.session_id.like(f"channel:{normalized_channel_id}:%"),
            )
        )
    elif normalized_source == "web":
        filters.append(not_(_is_channel_session_sql_predicate()))
    elif normalized_source == "channel":
        filters.append(_is_channel_session_sql_predicate())

    return filters


def _session_matches_list_filters(
    session_id: str,
    config: dict[str, Any] | None,
    *,
    channel_id: str | None,
    source: str | None,
) -> bool:
    channel_source = _derive_channel_source(session_id, config)
    normalized_channel_id = str(channel_id or "").strip()
    normalized_source = str(source or "").strip().lower()

    if normalized_channel_id:
        return channel_source.get("channel_id") == normalized_channel_id

    if normalized_source == "web":
        return not channel_source.get("is_channel_session")
    if normalized_source == "channel":
        return bool(channel_source.get("is_channel_session"))

    return True


def _derive_channel_source(session_id: str, config: dict[str, Any] | None) -> dict[str, Any]:
    channel = dict((config or {}).get("channel") or {})
    channel_id = str(channel.get("channel_id") or "").strip()
    channel_type = str(channel.get("channel_type") or "").strip()
    if channel_id or channel_type:
        return {
            "is_channel_session": True,
            "channel_id": channel_id or None,
            "channel_type": channel_type or None,
        }
    session_id_text = str(session_id or "")
    if session_id_text.startswith("channel:"):
        parts = session_id_text.split(":", 2)
        return {
            "is_channel_session": True,
            "channel_id": parts[1] if len(parts) > 1 and parts[1] else None,
            "channel_type": None,
        }
    return {
        "is_channel_session": False,
        "channel_id": None,
        "channel_type": None,
    }


def _message_dict(record: AssistantMessageRecord) -> dict[str, Any]:
    return {
        "message_id": record.message_id,
        "session_id": record.session_id,
        "role": record.role,
        "content": record.content,
        "created_at": _iso(record.created_at),
        "linked_attempt_id": record.linked_attempt_id,
        "metadata": dict(record.metadata_json or {}),
    }


def _event_dict(record: AssistantEventRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "session_id": record.session_id,
        "event_type": record.event_type,
        "payload": dict(record.payload or {}),
        "created_at": _iso(record.created_at),
    }


def _agent_dict(record) -> dict[str, Any]:
    tool_configs = normalize_tool_configs(
        getattr(record, "tool_configs_json", None),
        fallback_tool_names=getattr(record, "tool_names", None),
    )
    row = {
        "id": record.id,
        "name": record.name,
        "status": record.status,
        "system_prompt": record.system_prompt,
        "system_prompt_template_id": (
            str(getattr(record, "system_prompt_template_id", "") or "").strip() or None
        ),
        "model_route_name": record.model_route_name,
        "tool_configs": tool_configs,
        "tool_names": derive_tool_names(tool_configs),
        "skill_names": list(record.skill_names or []),
        "max_turns": record.max_turns,
        "is_default": bool(record.is_default),
        "is_builtin": bool(getattr(record, "is_builtin", False)),
        "context_compaction": normalize_context_compaction_config(
            getattr(record, "context_compaction_json", None)
        ),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }
    row["prompt_template_id"] = row["system_prompt_template_id"]
    # Pin code-controlled identity for the builtin main agent BEFORE resolving
    # the prompt, so resolution renders the locked main_agent.j2 template.
    apply_main_agent_overrides(row)
    row["resolved_system_prompt"] = resolve_agent_system_prompt(row)
    return row


def _channel_dict(record, *, include_secrets: bool = False) -> dict[str, Any]:
    secrets = dict(record.secrets or {})
    row = {
        "id": record.id,
        "name": record.name,
        "type": record.type,
        "enabled": bool(record.enabled),
        "agent_id": record.agent_id,
        "status": record.status,
        "last_error": record.last_error,
        "last_connected_at": _iso(record.last_connected_at) if record.last_connected_at else None,
        "config": dict(record.config or {}),
        "secret_keys": sorted(key for key, value in secrets.items() if value),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }
    if include_secrets:
        row["secrets"] = secrets
    return row


def _merge_secrets(existing: dict[str, Any], updates: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(existing or {})
    if not updates:
        return merged
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        merged[key] = value
    return merged


class InMemoryAssistantRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.traces: dict[str, list[dict[str, Any]]] = {}  # session_id -> traces
        self.peer_sessions: dict[tuple[str, str], str] = {}  # (channel_id, peer) -> active session

    async def create_session(
        self,
        *,
        agent_id: str,
        title: str,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utcnow().isoformat()
        session_id = session_id or f"asst-{uuid4().hex[:12]}"
        row = {
            "session_id": session_id,
            "agent_id": agent_id,
            "title": title,
            "status": "idle",
            "config": dict(config or {}),
            "created_at": now,
            "updated_at": now,
            "last_attempt_id": None,
        }
        self.sessions[session_id] = row
        self.messages[session_id] = []
        self.events[session_id] = []
        return self._session_row(row)

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
        channel_id: str | None = None,
        source: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [
            row
            for row in self.sessions.values()
            if _session_matches_list_filters(
                row["session_id"],
                row.get("config"),
                channel_id=channel_id,
                source=source,
            )
        ]
        rows = sorted(rows, key=lambda x: x["updated_at"], reverse=True)
        return [self._session_row(row) for row in rows[offset : offset + limit]], len(rows)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.sessions.get(session_id)
        return self._session_row(row) if row is not None else None

    async def get_active_peer_session(self, channel_id: str, peer_session_id: str) -> str | None:
        return self.peer_sessions.get((channel_id, peer_session_id))

    async def set_active_peer_session(
        self, channel_id: str, peer_session_id: str, active_session_id: str
    ) -> None:
        self.peer_sessions[(channel_id, peer_session_id)] = active_session_id

    async def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        last_attempt_id: str | None = None,
        agent_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        row = self.sessions.get(session_id)
        if row is None:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")
        if status is not None:
            row["status"] = status
        if last_attempt_id is not None:
            row["last_attempt_id"] = last_attempt_id
        if agent_id is not None:
            row["agent_id"] = agent_id
        if title is not None:
            row["title"] = title
        row["updated_at"] = _utcnow().isoformat()
        return self._session_row(row)

    async def update_session_config(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        row = self.sessions.get(session_id)
        if row is None:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")
        merged = dict(row.get("config") or {})
        merged.update(dict(patch or {}))
        row["config"] = merged
        row["updated_at"] = _utcnow().isoformat()
        return self._session_row(row)

    @staticmethod
    def _session_row(row: dict[str, Any]) -> dict[str, Any]:
        copied = dict(row)
        copied["config"] = dict(row.get("config") or {})
        copied["channel_source"] = _derive_channel_source(copied["session_id"], copied["config"])
        return copied

    async def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        linked_attempt_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session_id not in self.sessions:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")
        row = {
            "message_id": f"msg-{uuid4().hex[:12]}",
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": _utcnow().isoformat(),
            "linked_attempt_id": linked_attempt_id,
            "metadata": dict(metadata or {}),
            "deleted": False,
        }
        self.messages.setdefault(session_id, []).append(row)
        return dict(row)

    async def list_messages(self, session_id: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        all_msgs = [dict(row) for row in self.messages.get(session_id, []) if not row.get("deleted")]
        return all_msgs[offset : offset + limit]

    async def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if session_id not in self.sessions:
            raise RecordNotFoundError(f"assistant session not found: {session_id}")
        row = {
            "event_id": f"evt-{uuid4().hex[:12]}",
            "session_id": session_id,
            "event_type": event_type,
            "payload": dict(payload),
            "created_at": _utcnow().isoformat(),
            "deleted": False,
        }
        self.events.setdefault(session_id, []).append(row)
        return dict(row)

    async def list_events(
        self,
        session_id: str,
        *,
        after_id: str | None,
        limit: int,
        tail: bool = False,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.events.get(session_id, []) if not row.get("deleted")]
        if after_id:
            ids = [row["event_id"] for row in rows]
            if after_id in ids:
                rows = rows[ids.index(after_id) + 1 :]
            return [dict(row) for row in rows[:limit]]
        if tail:
            return [dict(row) for row in rows[-limit:]] if limit else [dict(row) for row in rows]
        return [dict(row) for row in rows[:limit]]

    async def rollback_attempt(self, session_id: str, attempt_id: str) -> None:
        """软删除该 attempt 的所有 messages 和 events"""
        for msg in self.messages.get(session_id, []):
            if msg.get("linked_attempt_id") == attempt_id:
                msg["deleted"] = True
        for evt in self.events.get(session_id, []):
            if evt.get("payload", {}).get("attempt_id") == attempt_id:
                evt["deleted"] = True

    async def list_traces(
        self,
        session_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self.traces.get(session_id, [])
        return [dict(row) for row in rows[offset : offset + limit]], len(rows)

    async def get_trace_detail(self, trace_id: str) -> dict[str, Any] | None:
        for traces in self.traces.values():
            for row in traces:
                if row.get("trace_id") == trace_id:
                    return dict(row)
        return None

    async def get_spans_for_sessions(
        self, session_ids: list[str],
    ) -> dict[str, Any]:
        # The in-memory repo only stores trace summaries (no per-span rows),
        # so we surface an empty payload here. Tests that exercise cron
        # trace aggregation should use the SQLAlchemy backend.
        return {"spans": [], "model_invocations": []}


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}

    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        rid = agent.get("id")
        if is_builtin_agent(rid) or bool(agent.get("is_builtin")):
            raise BuiltinAgentImmutableError(
                f"cannot create a builtin agent or reuse a fixed builtin id "
                f"({rid!r}); builtin agents are code-managed"
            )
        now = _utcnow().isoformat()
        row = {
            "id": agent.get("id") or f"agent-{uuid4().hex[:12]}",
            "name": agent["name"],
            "status": agent.get("status", "active"),
            "system_prompt": str(agent.get("system_prompt") or ""),
            "system_prompt_template_id": str(
                agent.get("prompt_template_id") or agent.get("system_prompt_template_id") or ""
            ).strip() or None,
            "model_route_name": agent.get("model_route_name", ""),
            "tool_configs": normalize_tool_configs(
                agent.get("tool_configs"),
                fallback_tool_names=agent.get("tool_names"),
            ),
            "skill_names": list(agent.get("skill_names") or []),
            "max_turns": int(agent.get("max_turns") or 6),
            "is_default": bool(agent.get("is_default", False)),
            "is_builtin": False,
            "context_compaction": normalize_context_compaction_config(
                agent.get("context_compaction")
            ),
            "created_at": now,
            "updated_at": now,
        }
        row["tool_names"] = derive_tool_names(row["tool_configs"])
        self.agents[row["id"]] = row
        return _copy_agent_row(row)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self.agents.get(agent_id)
        return _copy_agent_row(row) if row is not None else None

    async def list_agents(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        agents = self.agents.values()
        if not include_inactive:
            agents = (a for a in agents if a["status"] == "active")
        return sorted(
            (_copy_agent_row(a) for a in agents),
            key=lambda a: (not a.get("is_builtin"), not a["is_default"], a["created_at"]),
        )

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        row = self.agents.get(agent_id)
        if row is None:
            raise RecordNotFoundError(f"agent not found: {agent_id}")
        _reject_builtin_locked_updates(row, updates)
        for key in (
            "name",
            "status",
            "system_prompt",
            "system_prompt_template_id",
            "model_route_name",
            "max_turns",
            "is_default",
        ):
            if key in updates:
                row[key] = updates[key]
        if "prompt_template_id" in updates:
            row["system_prompt_template_id"] = str(updates.get("prompt_template_id") or "").strip() or None
        if "tool_configs" in updates or "tool_names" in updates:
            row["tool_configs"] = normalize_tool_configs(
                updates.get("tool_configs"),
                fallback_tool_names=updates.get("tool_names") if "tool_configs" not in updates else None,
            )
            row["tool_names"] = derive_tool_names(row["tool_configs"])
        if "skill_names" in updates:
            row["skill_names"] = list(updates.get("skill_names") or [])
        if "context_compaction" in updates:
            row["context_compaction"] = normalize_context_compaction_config(
                {
                    **normalize_context_compaction_config(row.get("context_compaction")),
                    **dict(updates.get("context_compaction") or {}),
                }
            )
        row["updated_at"] = _utcnow().isoformat()
        return _copy_agent_row(row)

    async def delete_agent(self, agent_id: str, *, force: bool = False) -> None:
        # The in-memory repo stores no sessions/channels, so there is nothing to
        # cascade — ``force`` is accepted for signature parity with the SQL repo.
        row = self.agents.get(agent_id)
        if row is None:
            raise RecordNotFoundError(f"agent not found: {agent_id}")
        _reject_builtin_delete(row)
        del self.agents[agent_id]

    async def clone_agent(self, source_agent_id: str, new_name: str) -> dict[str, Any]:
        source = self.agents.get(source_agent_id)
        if source is None:
            raise RecordNotFoundError(f"agent not found: {source_agent_id}")
        payload = dict(source)
        payload.update({
            "id": f"agent-{uuid4().hex[:12]}",
            "name": new_name,
            "is_default": False,
        })
        # ``create_agent`` rejects ``is_builtin`` — strip it; a clone is always a
        # plain editable agent. A builtin source seeds the clone with the
        # code-controlled defaults (all enabled skills + full tool registry +
        # main-agent template) as its starting point.
        payload.pop("is_builtin", None)
        if _agent_is_builtin(source):
            payload["skill_names"] = builtin_skill_names()
            payload["tool_configs"] = [
                {"name": name, "load_mode": "base"} for name in builtin_tool_names()
            ]
            payload["tool_names"] = list(builtin_tool_names())
            payload["system_prompt_template_id"] = MAIN_AGENT_PROMPT_TEMPLATE_ID
            payload["prompt_template_id"] = MAIN_AGENT_PROMPT_TEMPLATE_ID
            payload["system_prompt"] = ""
        return await self.create_agent(payload)

    async def ensure_main_agent(self) -> dict[str, Any]:
        """In-memory parity with the SQL repo's idempotent main-agent pin."""
        now = _utcnow().isoformat()
        row = self.agents.get(MAIN_AGENT_ID)
        if row is None:
            row = {
                "id": MAIN_AGENT_ID,
                "name": MAIN_AGENT_NAME,
                "status": "active",
                "system_prompt": "",
                "system_prompt_template_id": MAIN_AGENT_PROMPT_TEMPLATE_ID,
                "model_route_name": "",
                "tool_configs": [],
                "tool_names": [],
                "skill_names": [],
                "max_turns": 6,
                "is_default": True,
                "is_builtin": True,
                "context_compaction": normalize_context_compaction_config(None),
                "created_at": now,
                "updated_at": now,
            }
            self.agents[MAIN_AGENT_ID] = row
        else:
            # Re-pin identity; preserve editable knobs (route / compaction / turns).
            row.update({
                "name": MAIN_AGENT_NAME,
                "status": "active",
                "system_prompt": "",
                "system_prompt_template_id": MAIN_AGENT_PROMPT_TEMPLATE_ID,
                "is_default": True,
                "is_builtin": True,
                "updated_at": now,
            })
        return _copy_agent_row(row)

    async def ensure_signal_composer_agent(self) -> dict[str, Any]:
        """In-memory parity with the SQL repo's composer-agent pin.

        Companion to :meth:`ensure_main_agent`. Same idempotent contract:
        insert if missing, otherwise re-pin identity while preserving editable
        knobs. The composer carries empty tools/skills (compose-only). A blank
        model route inherits the main agent's route (parity with the SQL repo)
        so a fresh in-memory boot is callable too."""
        now = _utcnow().isoformat()
        main_row = self.agents.get(MAIN_AGENT_ID)
        inherited_route = (
            str(main_row.get("model_route_name", "") or "").strip()
            if main_row else ""
        )
        row = self.agents.get(SIGNAL_COMPOSER_AGENT_ID)
        if row is None:
            row = {
                "id": SIGNAL_COMPOSER_AGENT_ID,
                "name": SIGNAL_COMPOSER_AGENT_NAME,
                "status": "active",
                "system_prompt": "",
                "system_prompt_template_id": SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
                "model_route_name": inherited_route,
                "tool_configs": [],
                "tool_names": [],
                "skill_names": [],
                "max_turns": 6,
                "is_default": False,
                "is_builtin": True,
                "context_compaction": normalize_context_compaction_config(None),
                "created_at": now,
                "updated_at": now,
            }
            self.agents[SIGNAL_COMPOSER_AGENT_ID] = row
        else:
            row.update({
                "name": SIGNAL_COMPOSER_AGENT_NAME,
                "status": "active",
                "system_prompt": "",
                "system_prompt_template_id": SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
                # Blank route = no opinion → inherit main; explicit route preserved.
                "model_route_name": row.get("model_route_name") or inherited_route,
                # Keep tools/skills empty even if mutated — compose-only by design.
                "tool_configs": [],
                "tool_names": [],
                "skill_names": [],
                "is_default": False,
                "is_builtin": True,
                "updated_at": now,
            })
        return _copy_agent_row(row)


class _ChannelRow:
    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)


class InMemoryChannelRepository:
    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {}

    async def create_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        row = {
            "id": channel.get("id") or f"channel-{uuid4().hex[:12]}",
            "name": channel["name"],
            "type": channel["type"],
            "enabled": bool(channel.get("enabled", False)),
            "agent_id": channel["agent_id"],
            "status": channel.get("status") or ("disabled" if not channel.get("enabled", False) else "stopped"),
            "last_error": channel.get("last_error", ""),
            "last_connected_at": channel.get("last_connected_at"),
            "config": dict(channel.get("config") or {}),
            "secrets": dict(channel.get("secrets") or {}),
            "created_at": now,
            "updated_at": now,
        }
        self.channels[row["id"]] = row
        return _channel_dict(_ChannelRow(row))

    async def get_channel(
        self, channel_id: str, *, include_secrets: bool = False
    ) -> dict[str, Any] | None:
        row = self.channels.get(channel_id)
        return _channel_dict(_ChannelRow(row), include_secrets=include_secrets) if row else None

    async def list_channels(
        self,
        *,
        type: str | None = None,
        enabled: bool | None = None,
        agent_id: str | None = None,
        include_secrets: bool = False,
    ) -> list[dict[str, Any]]:
        rows = list(self.channels.values())
        if type is not None:
            rows = [row for row in rows if row["type"] == type]
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is enabled]
        if agent_id is not None:
            rows = [row for row in rows if row["agent_id"] == agent_id]
        rows.sort(key=lambda row: row["created_at"])
        return [
            _channel_dict(_ChannelRow(row), include_secrets=include_secrets)
            for row in rows
        ]

    async def update_channel(self, channel_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        row = self.channels.get(channel_id)
        if row is None:
            raise RecordNotFoundError(f"channel not found: {channel_id}")
        for key in ("name", "type", "enabled", "agent_id", "status", "last_error", "last_connected_at"):
            if key in updates:
                row[key] = updates[key]
        if "config" in updates:
            row["config"] = dict(updates.get("config") or {})
        if "secrets" in updates:
            row["secrets"] = _merge_secrets(row.get("secrets") or {}, updates.get("secrets") or {})
        row["updated_at"] = _utcnow()
        return _channel_dict(_ChannelRow(row))

    async def delete_channel(self, channel_id: str) -> None:
        if channel_id not in self.channels:
            raise RecordNotFoundError(f"channel not found: {channel_id}")
        del self.channels[channel_id]

    async def copy_secret(self, channel_id: str, secret_key: str) -> str:
        row = self.channels.get(channel_id)
        if row is None:
            raise RecordNotFoundError(f"channel not found: {channel_id}")
        value = (row.get("secrets") or {}).get(secret_key)
        if not value:
            raise RecordNotFoundError(f"channel secret not found: {secret_key}")
        return str(value)

    async def update_status(
        self,
        channel_id: str,
        *,
        status: str,
        last_error: str = "",
        last_connected_at: datetime | None = None,
    ) -> dict[str, Any]:
        return await self.update_channel(
            channel_id,
            {
                "status": status,
                "last_error": last_error,
                "last_connected_at": last_connected_at,
            },
        )


class SqlAlchemyAssistantRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_session(
        self,
        *,
        agent_id: str,
        title: str,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utcnow()
        record = AssistantSessionRecord(
            session_id=session_id or f"asst-{uuid4().hex[:12]}",
            agent_id=agent_id,
            title=title,
            status="idle",
            config=dict(config or {}),
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session:
            session.add(record)
            await session.commit()
            return _session_dict(record)

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
        channel_id: str | None = None,
        source: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = _build_list_sessions_filters(channel_id=channel_id, source=source)
        async with self.session_factory() as session:
            count_stmt = select(func.count()).select_from(AssistantSessionRecord)
            list_stmt = select(AssistantSessionRecord).order_by(AssistantSessionRecord.updated_at.desc())
            for clause in filters:
                count_stmt = count_stmt.where(clause)
                list_stmt = list_stmt.where(clause)
            total = int((await session.execute(count_stmt)).scalar_one())
            result = await session.execute(list_stmt.limit(limit).offset(offset))
            return [_session_dict(row) for row in result.scalars().all()], total

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(AssistantSessionRecord, session_id)
            return _session_dict(record) if record is not None else None

    async def get_active_peer_session(self, channel_id: str, peer_session_id: str) -> str | None:
        async with self.session_factory() as session:
            record = await session.get(ChannelPeerSessionRecord, (channel_id, peer_session_id))
            return record.active_session_id if record is not None else None

    async def set_active_peer_session(
        self, channel_id: str, peer_session_id: str, active_session_id: str
    ) -> None:
        async with self.session_factory() as session:
            record = await session.get(ChannelPeerSessionRecord, (channel_id, peer_session_id))
            if record is None:
                session.add(
                    ChannelPeerSessionRecord(
                        channel_id=channel_id,
                        peer_session_id=peer_session_id,
                        active_session_id=active_session_id,
                        updated_at=_utcnow(),
                    )
                )
            else:
                record.active_session_id = active_session_id
                record.updated_at = _utcnow()
            await session.commit()

    async def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        last_attempt_id: str | None = None,
        agent_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.get(AssistantSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"assistant session not found: {session_id}")
            if status is not None:
                record.status = status
            if last_attempt_id is not None:
                record.last_attempt_id = last_attempt_id
            if agent_id is not None:
                record.agent_id = agent_id
            if title is not None:
                record.title = title
            record.updated_at = _utcnow()
            await session.commit()
            return _session_dict(record)

    async def update_session_config(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.get(AssistantSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"assistant session not found: {session_id}")
            merged = dict(record.config or {})
            merged.update(dict(patch or {}))
            record.config = merged
            record.updated_at = _utcnow()
            await session.commit()
            return _session_dict(record)

    async def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        linked_attempt_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            owner = await session.get(AssistantSessionRecord, session_id)
            if owner is None:
                raise RecordNotFoundError(f"assistant session not found: {session_id}")
            now = _utcnow()
            record = AssistantMessageRecord(
                message_id=f"msg-{uuid4().hex[:12]}",
                session_id=session_id,
                role=role,
                content=content,
                linked_attempt_id=linked_attempt_id,
                metadata_json=dict(metadata or {}),
                created_at=now,
            )
            owner.updated_at = now
            session.add(record)
            await session.commit()
            return _message_dict(record)

    async def list_messages(self, session_id: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AssistantMessageRecord)
                .where(AssistantMessageRecord.session_id == session_id)
                .where(AssistantMessageRecord.deleted == False)
                .order_by(AssistantMessageRecord.created_at, AssistantMessageRecord.message_id)
                .limit(limit)
                .offset(offset)
            )
            return [_message_dict(row) for row in result.scalars().all()]

    async def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            owner = await session.get(AssistantSessionRecord, session_id)
            if owner is None:
                raise RecordNotFoundError(f"assistant session not found: {session_id}")
            now = _utcnow()
            record = AssistantEventRecord(
                event_id=f"evt-{uuid4().hex[:12]}",
                session_id=session_id,
                event_type=event_type,
                payload=dict(payload),
                created_at=now,
            )
            owner.updated_at = now
            session.add(record)
            await session.commit()
            return _event_dict(record)

    async def list_events(
        self,
        session_id: str,
        *,
        after_id: str | None,
        limit: int,
        tail: bool = False,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            marker: int | None = None
            if after_id:
                marker = (
                    await session.execute(
                        select(AssistantEventRecord.id).where(
                            AssistantEventRecord.event_id == after_id,
                            AssistantEventRecord.session_id == session_id,
                        )
                    )
                ).scalar_one_or_none()
            if tail and marker is None:
                # No forward marker: caller wants the most recent `limit` events
                # (e.g. reconstructing "is a run currently in flight" state on
                # page load), not the oldest `limit` — a plain ascending
                # `.limit()` here would silently hand back the earliest slice
                # of a long session and never reach the in-flight attempt.
                # Fetch newest-first, then restore chronological order.
                stmt = (
                    select(AssistantEventRecord)
                    .where(AssistantEventRecord.session_id == session_id)
                    .where(AssistantEventRecord.deleted == False)
                    .order_by(AssistantEventRecord.id.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = list(result.scalars().all())
                rows.reverse()
                return [_event_dict(row) for row in rows]
            stmt = (
                select(AssistantEventRecord)
                .where(AssistantEventRecord.session_id == session_id)
                .where(AssistantEventRecord.deleted == False)
                .order_by(AssistantEventRecord.id)
                .limit(limit)
            )
            if marker is not None:
                stmt = stmt.where(AssistantEventRecord.id > marker)
            result = await session.execute(stmt)
            return [_event_dict(row) for row in result.scalars().all()]

    async def rollback_attempt(self, session_id: str, attempt_id: str) -> None:
        """软删除该 attempt 的所有 messages 和 events"""
        async with self.session_factory() as session:
            await session.execute(
                update(AssistantMessageRecord)
                .where(AssistantMessageRecord.session_id == session_id)
                .where(AssistantMessageRecord.linked_attempt_id == attempt_id)
                .values(deleted=True)
            )
            await session.execute(
                update(AssistantEventRecord)
                .where(AssistantEventRecord.session_id == session_id)
                .where(cast(AssistantEventRecord.payload["attempt_id"], String) == attempt_id)
                .values(deleted=True)
            )
            await session.commit()

    async def list_traces(
        self,
        session_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        async with self.session_factory() as session:
            # Count distinct trace_ids for this session
            total = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(DebugSessionSpanRecord.trace_id)).label("cnt")).where(
                            DebugSessionSpanRecord.session_id == session_id
                        )
                    )
                ).scalar_one()
            )
            # Root spans: assistant.loop spans (identified by name + session_id).
            # Note: parent_span_id IS NULL does NOT work here because assistant.loop
            # is created inside FastAPI's span context, so it inherits FastAPI's span_id as parent.
            root_spans_subq = (
                select(DebugSessionSpanRecord)
                .where(DebugSessionSpanRecord.session_id == session_id)
                .where(DebugSessionSpanRecord.name == "assistant.loop")
                .order_by(DebugSessionSpanRecord.start_time.desc())
            ).subquery()
            # Count total spans per trace
            span_counts = (
                select(
                    DebugSessionSpanRecord.trace_id,
                    func.count().label("span_count"),
                )
                .where(DebugSessionSpanRecord.session_id == session_id)
                .group_by(DebugSessionSpanRecord.trace_id)
            ).subquery()
            # Model invocation summary per trace
            model_inv_summary = (
                select(
                    ModelInvocationRecord.trace_id,
                    func.max(ModelInvocationRecord.model).label("model"),
                    func.sum(ModelInvocationRecord.input_tokens).label("input_tokens"),
                    func.sum(ModelInvocationRecord.output_tokens).label("output_tokens"),
                    func.sum(ModelInvocationRecord.cache_read_tokens).label("cache_read_tokens"),
                    func.sum(ModelInvocationRecord.cache_write_tokens).label("cache_write_tokens"),
                    func.max(ModelInvocationRecord.error_message).label("error_message"),
                )
                .where(ModelInvocationRecord.trace_id.isnot(None))
                .group_by(ModelInvocationRecord.trace_id)
            ).subquery()
            # Full query: root spans + span count + model invocation summary
            result = await session.execute(
                select(
                    root_spans_subq.c.trace_id,
                    root_spans_subq.c.session_id,
                    root_spans_subq.c.name.label("span_name"),
                    root_spans_subq.c.start_time.label("created_at"),
                    root_spans_subq.c.duration_ms,
                    root_spans_subq.c.status,
                    span_counts.c.span_count,
                    model_inv_summary.c.model,
                    model_inv_summary.c.input_tokens,
                    model_inv_summary.c.output_tokens,
                    model_inv_summary.c.cache_read_tokens,
                    model_inv_summary.c.cache_write_tokens,
                    model_inv_summary.c.error_message,
                )
                .outerjoin(span_counts, root_spans_subq.c.trace_id == span_counts.c.trace_id)
                .outerjoin(model_inv_summary, root_spans_subq.c.trace_id == model_inv_summary.c.trace_id)
                .order_by(root_spans_subq.c.start_time.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = result.all()
            return (
                [
                    {
                        "trace_id": row.trace_id,
                        "session_id": row.session_id,
                        "span_name": row.span_name,
                        "created_at": _iso(row.created_at),
                        "duration_ms": row.duration_ms,
                        "status": row.status,
                        "span_count": row.span_count or 0,
                        "model": row.model,
                        "input_tokens": int(row.input_tokens) if row.input_tokens else None,
                        "output_tokens": int(row.output_tokens) if row.output_tokens else None,
                        "cache_read_tokens": int(row.cache_read_tokens) if row.cache_read_tokens else None,
                        "cache_write_tokens": int(row.cache_write_tokens) if row.cache_write_tokens else None,
                        "error_message": row.error_message,
                    }
                    for row in rows
                ],
                total,
            )

    async def get_spans_for_sessions(
        self, session_ids: list[str],
    ) -> dict[str, Any]:
        """Aggregate spans + model_invocations across a set of debug sessions.

        Used by the cron-run trace endpoint: one cron fire may touch
        multiple sessions (the agent's LLM session for the composed reply,
        plus any per-instance ``cycle_runs`` sessions for
        ``strategy_signal_alert``). Returning them in one payload lets the
        frontend render a single trace tree per run.
        """

        cleaned = sorted({s for s in session_ids if isinstance(s, str) and s.strip()})
        if not cleaned:
            return {"spans": [], "model_invocations": []}

        async with self.session_factory() as session:
            spans_result = await session.execute(
                select(DebugSessionSpanRecord)
                .where(DebugSessionSpanRecord.session_id.in_(cleaned))
                .order_by(DebugSessionSpanRecord.start_time)
            )
            spans = [
                {
                    "span_id": row.span_id,
                    "trace_id": row.trace_id,
                    "parent_span_id": row.parent_span_id,
                    "session_id": row.session_id,
                    "name": row.name,
                    "span_type": row.span_type,
                    "start_time": _iso(row.start_time),
                    "end_time": _iso(row.end_time) if row.end_time else None,
                    "duration_ms": row.duration_ms,
                    "attributes": dict(row.attributes),
                    "status": row.status,
                    "span_source": row.span_source,
                }
                for row in spans_result.scalars().all()
            ]
            trace_ids = sorted({s["trace_id"] for s in spans if s.get("trace_id")})
            model_invocations: list[dict[str, Any]] = []
            if trace_ids:
                inv_result = await session.execute(
                    select(ModelInvocationRecord)
                    .where(ModelInvocationRecord.trace_id.in_(trace_ids))
                    .order_by(ModelInvocationRecord.created_at)
                )
                model_invocations = [
                    {
                        "id": row.id,
                        "model_id": row.model_id,
                        "provider_kind": row.provider_kind,
                        "model_route_name": row.model_route_name,
                        "provider_key": row.provider_key,
                        "model": row.model,
                        "task_id": row.task_id,
                        "run_id": row.run_id,
                        "trace_id": row.trace_id,
                        "span_id": row.span_id,
                        "call_kind": row.call_kind,
                        "first_token_latency_ms": row.first_token_latency_ms,
                        "total_latency_ms": row.total_latency_ms,
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "total_tokens": row.total_tokens,
                        "cache_read_tokens": row.cache_read_tokens,
                        "cache_write_tokens": row.cache_write_tokens,
                        "ok": bool(row.ok) if row.ok is not None else True,
                        "error_message": row.error_message,
                        "created_at": _iso(row.created_at),
                        "request": dict(row.request_payload) if row.request_payload is not None else {},
                        "response": dict(row.response_payload) if row.response_payload is not None else None,
                    }
                    for row in inv_result.scalars().all()
                ]
        return {"spans": spans, "model_invocations": model_invocations}

    async def get_trace_detail(self, trace_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            # Get all spans for this trace_id
            spans_result = await session.execute(
                select(DebugSessionSpanRecord)
                .where(DebugSessionSpanRecord.trace_id == trace_id)
                .order_by(DebugSessionSpanRecord.start_time)
            )
            spans = [
                {
                    "span_id": row.span_id,
                    "trace_id": row.trace_id,
                    "parent_span_id": row.parent_span_id,
                    "session_id": row.session_id,
                    "name": row.name,
                    "span_type": row.span_type,
                    "start_time": _iso(row.start_time),
                    "end_time": _iso(row.end_time) if row.end_time else None,
                    "duration_ms": row.duration_ms,
                    "attributes": dict(row.attributes),
                    "status": row.status,
                    "span_source": row.span_source,
                }
                for row in spans_result.scalars().all()
            ]
            if not spans:
                return None
            # Get all model invocations for this trace_id
            inv_result = await session.execute(
                select(ModelInvocationRecord)
                .where(ModelInvocationRecord.trace_id == trace_id)
                .order_by(ModelInvocationRecord.created_at)
            )
            model_invocations = [
                {
                    "id": row.id,
                    "model_id": row.model_id,
                    "provider_kind": row.provider_kind,
                    "model_route_name": row.model_route_name,
                    "provider_key": row.provider_key,
                    "model": row.model,
                    "task_id": row.task_id,
                    "run_id": row.run_id,
                    "trace_id": row.trace_id,
                    "span_id": row.span_id,
                    "call_kind": row.call_kind,
                    "first_token_latency_ms": row.first_token_latency_ms,
                    "total_latency_ms": row.total_latency_ms,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "total_tokens": row.total_tokens,
                    "cache_read_tokens": row.cache_read_tokens,
                    "cache_write_tokens": row.cache_write_tokens,
                    "ok": bool(row.ok) if row.ok is not None else True,
                    "error_message": row.error_message,
                    "created_at": _iso(row.created_at),
                    "request": dict(row.request_payload) if row.request_payload is not None else {},
                    "response": dict(row.response_payload) if row.response_payload is not None else None,
                }
                for row in inv_result.scalars().all()
            ]
            return {
                "trace_id": trace_id,
                "session_id": spans[0]["session_id"],
                "spans": spans,
                "model_invocations": model_invocations,
            }


class SqlAlchemyAgentRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        # The fixed builtin agents are owned by code (ensure_main_agent /
        # ensure_signal_composer_agent). Block any attempt to create a second
        # builtin or to collide with their well-known ids.
        rid = agent.get("id")
        if is_builtin_agent(rid) or bool(agent.get("is_builtin")):
            raise BuiltinAgentImmutableError(
                f"cannot create a builtin agent or reuse a fixed builtin id "
                f"({rid!r}); builtin agents are code-managed"
            )
        now = _utcnow()
        record = AgentRecord(
            id=agent.get("id") or f"agent-{uuid4().hex[:12]}",
            name=agent["name"],
            status=agent.get("status", "active"),
            system_prompt=agent["system_prompt"],
            system_prompt_template_id=str(
                agent.get("prompt_template_id") or agent.get("system_prompt_template_id") or ""
            ).strip() or None,
            model_route_name=agent.get("model_route_name", ""),
            tool_names=derive_tool_names(
                normalize_tool_configs(
                    agent.get("tool_configs"),
                    fallback_tool_names=agent.get("tool_names"),
                )
            ),
            tool_configs_json=normalize_tool_configs(
                agent.get("tool_configs"),
                fallback_tool_names=agent.get("tool_names"),
            ),
            skill_names=list(agent.get("skill_names") or []),
            max_turns=int(agent.get("max_turns") or 6),
            is_default=bool(agent.get("is_default", False)),
            is_builtin=False,
            context_compaction_json=normalize_context_compaction_config(
                agent.get("context_compaction")
            ),
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session:
            session.add(record)
            await session.commit()
            return _agent_dict(record)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            return _agent_dict(record) if record is not None else None

    async def list_agents(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = select(AgentRecord).order_by(
                AgentRecord.is_builtin.desc(),
                AgentRecord.is_default.desc(),
                AgentRecord.created_at,
            )
            result = await session.execute(stmt)
            agents = result.scalars().all()
            if not include_inactive:
                agents = [a for a in agents if a.status == "active"]
            return [_agent_dict(a) for a in agents]

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            if record is None:
                raise RecordNotFoundError(f"agent not found: {agent_id}")
            _reject_builtin_locked_updates(record, updates)
            for key in ("name", "status", "system_prompt", "system_prompt_template_id", "model_route_name",
                        "skill_names", "max_turns"):
                if key in updates:
                    setattr(record, key, updates[key])
            if "prompt_template_id" in updates:
                record.system_prompt_template_id = (
                    str(updates.get("prompt_template_id") or "").strip() or None
                )
            if "tool_configs" in updates or "tool_names" in updates:
                tool_configs = normalize_tool_configs(
                    updates.get("tool_configs"),
                    fallback_tool_names=updates.get("tool_names") if "tool_configs" not in updates else None,
                )
                record.tool_configs_json = tool_configs
                record.tool_names = derive_tool_names(tool_configs)
            if "is_default" in updates and updates["is_default"]:
                record.is_default = True
            if "context_compaction" in updates:
                record.context_compaction_json = normalize_context_compaction_config(
                    {
                        **normalize_context_compaction_config(record.context_compaction_json),
                        **dict(updates.get("context_compaction") or {}),
                    }
                )
            record.updated_at = _utcnow()
            await session.commit()
            return _agent_dict(record)

    async def delete_agent(self, agent_id: str, *, force: bool = False) -> None:
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            if record is None:
                raise RecordNotFoundError(f"agent not found: {agent_id}")
            _reject_builtin_delete(record)

            # Channels reference agents via ondelete=RESTRICT and are driven by a
            # live ChannelManager — tearing them down here is out of scope for a
            # session cascade. Block (even with force) so the operator stops the
            # channels explicitly rather than orphaning a running connector.
            channel_count = await session.scalar(
                select(func.count())
                .select_from(ChannelRecord)
                .where(ChannelRecord.agent_id == agent_id)
            )
            if channel_count:
                raise AgentInUseError(
                    f"agent {agent_id} still has {channel_count} channel(s); "
                    f"delete or reassign those channels before deleting the agent"
                )

            # assistant_sessions references agents via ondelete=RESTRICT to keep
            # session history intact. Detect the conflict up front and surface a
            # clear, actionable error instead of letting the DB raise a raw
            # IntegrityError (which would bubble up as an opaque 500).
            session_ids = list(
                await session.scalars(
                    select(AssistantSessionRecord.session_id).where(
                        AssistantSessionRecord.agent_id == agent_id
                    )
                )
            )
            if session_ids and not force:
                raise AgentInUseError(
                    f"agent {agent_id} still has {len(session_ids)} assistant "
                    f"session(s); delete or reassign those sessions before "
                    f"deleting the agent, or pass force=true to delete the agent "
                    f"and all of its sessions"
                )

            if session_ids:
                # assistant_messages / assistant_events carry session_id without a
                # DB-level FK, so they would be orphaned by a session delete —
                # remove them explicitly. assistant_loaded_skills has an FK CASCADE
                # but SQLite does not enforce it unless PRAGMA foreign_keys is on,
                # so delete it explicitly too for backend-independent behaviour.
                for child_model in (
                    AssistantMessageRecord,
                    AssistantEventRecord,
                    AssistantLoadedSkillRecord,
                ):
                    await session.execute(
                        delete(child_model).where(
                            child_model.session_id.in_(session_ids)
                        )
                    )
                await session.execute(
                    delete(AssistantSessionRecord).where(
                        AssistantSessionRecord.agent_id == agent_id
                    )
                )
                logger.warning(
                    "force-deleting agent %s cascaded %d assistant session(s) "
                    "and their messages/events/loaded-skills",
                    agent_id,
                    len(session_ids),
                )

            await session.delete(record)
            await session.commit()

    async def clone_agent(self, source_agent_id: str, new_name: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            source = await session.get(AgentRecord, source_agent_id)
            if source is None:
                raise RecordNotFoundError(f"agent not found: {source_agent_id}")
            now = _utcnow()
            if _agent_is_builtin(source):
                # Cloning the fixed main agent yields an ordinary editable agent
                # seeded with the code-controlled defaults (all enabled skills +
                # full tool registry, main-agent template as a starting point).
                clone_skill_names = builtin_skill_names()
                clone_tool_configs = [
                    {"name": name, "load_mode": "base"} for name in builtin_tool_names()
                ]
                clone_template_id: str | None = MAIN_AGENT_PROMPT_TEMPLATE_ID
                clone_system_prompt = ""
            else:
                clone_skill_names = list(source.skill_names or [])
                clone_tool_configs = normalize_tool_configs(
                    source.tool_configs_json,
                    fallback_tool_names=source.tool_names,
                )
                clone_template_id = source.system_prompt_template_id
                clone_system_prompt = source.system_prompt
            new_record = AgentRecord(
                id=f"agent-{uuid4().hex[:12]}",
                name=new_name,
                status=source.status,
                system_prompt=clone_system_prompt,
                system_prompt_template_id=clone_template_id,
                model_route_name=source.model_route_name,
                tool_names=derive_tool_names(clone_tool_configs),
                tool_configs_json=clone_tool_configs,
                skill_names=clone_skill_names,
                max_turns=source.max_turns,
                is_default=False,
                is_builtin=False,
                context_compaction_json=normalize_context_compaction_config(
                    source.context_compaction_json
                ),
                created_at=now,
                updated_at=now,
            )
            session.add(new_record)
            await session.commit()
            return _agent_dict(new_record)

    async def ensure_main_agent(self) -> dict[str, Any]:
        """Idempotently pin the code-fixed builtin main agent on boot.

        Inserts the row if missing; otherwise re-pins the code-controlled identity
        (name / prompt template / ``is_default`` / ``is_builtin`` / ``status``)
        while PRESERVING the user-editable knobs (``model_route_name`` /
        ``context_compaction`` / ``max_turns``). Skills/tools are left empty in DB
        and expanded to the full set at runtime by the service load points, so the
        DB never carries a stale snapshot. Replaces the old empty-table-only seed,
        closing the "row was mutated / went missing → never repaired" gap.
        """
        async with self.session_factory() as session:
            now = _utcnow()
            record = await session.get(AgentRecord, MAIN_AGENT_ID)
            if record is None:
                record = AgentRecord(
                    id=MAIN_AGENT_ID,
                    name=MAIN_AGENT_NAME,
                    status="active",
                    system_prompt="",
                    system_prompt_template_id=MAIN_AGENT_PROMPT_TEMPLATE_ID,
                    model_route_name="",
                    tool_names=[],
                    tool_configs_json=[],
                    skill_names=[],
                    max_turns=6,
                    is_default=True,
                    is_builtin=True,
                    context_compaction_json=normalize_context_compaction_config(None),
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
            else:
                # Re-pin code-controlled identity; never touch the editable knobs
                # (model_route_name / context_compaction_json / max_turns).
                record.name = MAIN_AGENT_NAME
                record.status = "active"
                record.system_prompt = ""
                record.system_prompt_template_id = MAIN_AGENT_PROMPT_TEMPLATE_ID
                record.is_default = True
                record.is_builtin = True
                record.updated_at = now
            await session.commit()
            return _agent_dict(record)

    async def ensure_signal_composer_agent(self) -> dict[str, Any]:
        """Idempotently pin the code-fixed signal-card composer agent on boot.

        Companion to :meth:`ensure_main_agent` for the prose-push composer. Same
        contract: inserts the row if missing, otherwise re-pins the
        code-controlled identity (name / prompt template / ``is_builtin`` /
        ``status``) while PRESERVING the user-editable knobs
        (``model_route_name`` / ``context_compaction`` / ``max_turns``).

        Deliberately seeded with EMPTY tools and EMPTY skills — this agent is
        compose-only. Unlike the main agent, the service load points do NOT
        expand anything for it (``_resolve_tool_inventory`` /
        ``_compose_effective_system_prompt`` only short-circuit the *main*
        agent to the full registry), so the empty DB row is the truth at
        runtime too. ``is_default`` stays False: this agent never serves
        general routing, only explicit prose-compose turns.

        Model route inheritance: a freshly seeded composer has an empty
        ``model_route_name``; an empty route resolves to the baseline default,
        which on a real install has no API key and would 500 on the compose
        turn. So at seed/re-pin time, if the composer's route is empty it
        inherits the main agent's configured route (the same model the prose
        turn used to run on). An operator who sets an explicit route on the
        composer keeps it — re-pin only fills in the blank.
        """
        async with self.session_factory() as session:
            now = _utcnow()
            # Inherit the main agent's model route when the composer's is blank,
            # so a fresh seed is callable without extra config. Resolved in the
            # same session/transaction so boot ordering is race-free.
            main_record = await session.get(AgentRecord, MAIN_AGENT_ID)
            inherited_route = (
                str(getattr(main_record, "model_route_name", "") or "").strip()
                if main_record is not None else ""
            )
            record = await session.get(AgentRecord, SIGNAL_COMPOSER_AGENT_ID)
            if record is None:
                record = AgentRecord(
                    id=SIGNAL_COMPOSER_AGENT_ID,
                    name=SIGNAL_COMPOSER_AGENT_NAME,
                    status="active",
                    system_prompt="",
                    system_prompt_template_id=SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
                    model_route_name=inherited_route,
                    tool_names=[],
                    tool_configs_json=[],
                    skill_names=[],
                    max_turns=6,
                    is_default=False,
                    is_builtin=True,
                    context_compaction_json=normalize_context_compaction_config(None),
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
            else:
                # Re-pin code-controlled identity; preserve the editable knobs
                # (context_compaction_json / max_turns). model_route_name is also
                # editable, but a blank route is "no opinion" → inherit main.
                record.name = SIGNAL_COMPOSER_AGENT_NAME
                record.status = "active"
                record.system_prompt = ""
                record.system_prompt_template_id = SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID
                if not str(record.model_route_name or "").strip():
                    record.model_route_name = inherited_route
                # Keep tools/skills empty even if an operator mutated them: the
                # composer is compose-only by design (CLAUDE.md §错误可见性 —
                # never let a stale DB row re-open the tool surface silently).
                record.tool_names = []
                record.tool_configs_json = []
                record.skill_names = []
                record.is_default = False
                record.is_builtin = True
                record.updated_at = now
            await session.commit()
            return _agent_dict(record)


class SqlAlchemyChannelRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        now = _utcnow()
        record = ChannelRecord(
            id=channel.get("id") or f"channel-{uuid4().hex[:12]}",
            name=channel["name"],
            type=channel["type"],
            enabled=bool(channel.get("enabled", False)),
            agent_id=channel["agent_id"],
            status=channel.get("status") or ("disabled" if not channel.get("enabled", False) else "stopped"),
            last_error=channel.get("last_error", ""),
            last_connected_at=channel.get("last_connected_at"),
            config=dict(channel.get("config") or {}),
            secrets=dict(channel.get("secrets") or {}),
            created_at=now,
            updated_at=now,
        )
        async with self.session_factory() as session:
            session.add(record)
            await session.commit()
            return _channel_dict(record)

    async def get_channel(
        self, channel_id: str, *, include_secrets: bool = False
    ) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(ChannelRecord, channel_id)
            return _channel_dict(record, include_secrets=include_secrets) if record else None

    async def list_channels(
        self,
        *,
        type: str | None = None,
        enabled: bool | None = None,
        agent_id: str | None = None,
        include_secrets: bool = False,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = select(ChannelRecord).order_by(ChannelRecord.created_at)
            if type is not None:
                stmt = stmt.where(ChannelRecord.type == type)
            if enabled is not None:
                stmt = stmt.where(ChannelRecord.enabled.is_(enabled))
            if agent_id is not None:
                stmt = stmt.where(ChannelRecord.agent_id == agent_id)
            result = await session.execute(stmt)
            return [
                _channel_dict(record, include_secrets=include_secrets)
                for record in result.scalars().all()
            ]

    async def update_channel(self, channel_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            record = await session.get(ChannelRecord, channel_id)
            if record is None:
                raise RecordNotFoundError(f"channel not found: {channel_id}")
            for key in ("name", "type", "enabled", "agent_id", "status", "last_error", "last_connected_at"):
                if key in updates:
                    setattr(record, key, updates[key])
            if "config" in updates:
                record.config = dict(updates.get("config") or {})
            if "secrets" in updates:
                record.secrets = _merge_secrets(record.secrets or {}, updates.get("secrets") or {})
            record.updated_at = _utcnow()
            await session.commit()
            return _channel_dict(record)

    async def delete_channel(self, channel_id: str) -> None:
        async with self.session_factory() as session:
            record = await session.get(ChannelRecord, channel_id)
            if record is None:
                raise RecordNotFoundError(f"channel not found: {channel_id}")
            await session.delete(record)
            await session.commit()

    async def copy_secret(self, channel_id: str, secret_key: str) -> str:
        async with self.session_factory() as session:
            record = await session.get(ChannelRecord, channel_id)
            if record is None:
                raise RecordNotFoundError(f"channel not found: {channel_id}")
            value = (record.secrets or {}).get(secret_key)
            if not value:
                raise RecordNotFoundError(f"channel secret not found: {secret_key}")
            return str(value)

    async def update_status(
        self,
        channel_id: str,
        *,
        status: str,
        last_error: str = "",
        last_connected_at: datetime | None = None,
    ) -> dict[str, Any]:
        return await self.update_channel(
            channel_id,
            {
                "status": status,
                "last_error": last_error,
                "last_connected_at": last_connected_at,
            },
        )
