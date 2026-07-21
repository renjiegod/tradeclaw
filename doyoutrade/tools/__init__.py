"""Assistant tools package."""
from __future__ import annotations

import asyncio as _asyncio
import inspect as _inspect
import json as _json
import os as _os
import traceback as _traceback
from abc import ABC, abstractmethod as _abstractmethod
from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path
from typing import Any as _Any, Awaitable as _Awaitable, Callable as _Callable, TypeVar as _TypeVar, Union as _Union

# Alias so the registry body can use a concise name without the leading
# underscore mangling readability at the call site.
_inspect_isawaitable = _inspect.isawaitable

from doyoutrade.money.decimal_helpers import json_default_with_decimals
from doyoutrade.persistence.errors import RecordNotFoundError as _RecordNotFoundError
from doyoutrade.skills import load_skills
from doyoutrade.tools.storage import ToolResultStorage

_T = _TypeVar("_T")

MAX_TEXT_LENGTH = 100_000
TERMINAL_BACKTEST_STATUSES = frozenset({"completed", "finished", "failed"})

_TRACEBACK_TAIL_MAX_CHARS = 400
_STRATEGY_ID_PREFIXES = {
    "sd-": "strategy_definition",
}


def _json_dumps(data: _Any) -> str:
    return _json.dumps(data, ensure_ascii=False, default=json_default_with_decimals)


def _exception_metadata(exc: BaseException) -> dict[str, _Any]:
    """Return structured metadata that always carries a non-empty error message.

    Empty ``str(exc)`` (e.g. bare ``RuntimeError()`` / ``CancelledError``) used to
    surface as ``error: ""`` which left agents with no way to recover. We always
    attach the exception class name plus a short traceback tail so failure paths
    stay debuggable.
    """

    raw = str(exc).strip()
    error_type = type(exc).__name__
    message = raw if raw else f"{error_type} (no message)"
    tb = "".join(_traceback.format_exception_only(type(exc), exc)).strip()
    if not tb:
        tb = error_type
    if len(tb) > _TRACEBACK_TAIL_MAX_CHARS:
        tb = tb[-_TRACEBACK_TAIL_MAX_CHARS:]
    return {
        "error": message,
        "error_type": error_type,
        "traceback_tail": tb,
    }


def _classify_strategy_identifier(identifier: _Any) -> tuple[str, str] | None:
    """Detect ``sd-...`` ids that were sent into a task tool by mistake."""

    if not isinstance(identifier, str):
        return None
    for prefix, kind in _STRATEGY_ID_PREFIXES.items():
        if identifier.startswith(prefix):
            return prefix, kind
    return None


def wrong_identifier_type_error(identifier: str) -> dict[str, _Any] | None:
    """Return a structured error dict when a strategy id is passed to a task tool."""

    classified = _classify_strategy_identifier(identifier)
    if classified is None:
        return None
    prefix, kind = classified
    repair_tool = "get_strategy_definition"
    kind_label = "strategy definition"
    return {
        "status": "error",
        "error_code": "wrong_identifier_type",
        "error_type": "WrongIdentifierType",
        "error": (
            f"got {identifier!r} which looks like a {kind_label} id "
            f"(prefix {prefix!r}), not a task id"
        ),
        "repair_hints": [
            f"call {repair_tool} for {kind_label} resources",
            "use list_tasks or get_task with a real task_id (uuid-style)",
        ],
    }


_TASK_NOT_FOUND_HINT = (
    "identifier must be a task_id (UUID) or an exact task name; "
    "use list_tasks(q=...) to discover the task_id"
)


def _generic_failure_payload(exc: BaseException) -> dict[str, _Any]:
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "error": str(exc) or f"{type(exc).__name__} (no message)",
    }


def task_not_found_payload(identifier: str) -> dict[str, _Any]:
    """Structured ``task not found`` response with a recovery hint.

    Returned when ``identifier`` is neither a known task_id nor an exact
    task name; gives the model an explicit path back via ``list_tasks``
    instead of leaving the bare ``RecordNotFoundError`` text on screen.
    """

    return {
        "status": "error",
        "error_type": "RecordNotFoundError",
        "error_code": "task_not_found",
        "error": f"task not found: {identifier}",
        "hint": _TASK_NOT_FOUND_HINT,
    }


async def _try_resolve_task_id_by_name(
    svc: _Any,
    identifier: str,
) -> tuple[str | None, dict[str, _Any] | None]:
    """Resolve ``identifier`` as an exact task name via ``list_tasks_summary``.

    The platform service performs primary-key (task_id) lookups only, so
    when the model passes a task name the underlying call raises
    ``RecordNotFoundError``. Tools call this helper after that failure to
    salvage the call without a round-trip back to the model.

    Returns ``(task_id, error)``:
      * ``(task_id, None)`` — exactly one task with this exact name
      * ``(None, error_dict)`` — multiple tasks share the name (caller
        should return ``error_dict`` so the model can pick a candidate)
      * ``(None, None)`` — zero exact-name matches (caller should fall
        through to ``task_not_found_payload``)
    """

    try:
        page = await svc.list_tasks_summary(
            q=identifier, status=None, mode=None, limit=50, offset=0
        )
    except TypeError:
        # Older service contracts may not accept all keyword filters.
        try:
            page = await svc.list_tasks_summary(q=identifier, limit=50, offset=0)
        except Exception:
            return None, None
    except Exception:
        return None, None
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        return None, None
    exact = [
        item
        for item in items
        if isinstance(item, dict) and item.get("name") == identifier
    ]
    if len(exact) == 1:
        resolved = exact[0].get("task_id")
        if isinstance(resolved, str) and resolved:
            return resolved, None
        return None, None
    if len(exact) > 1:
        candidates = [
            {
                "task_id": item.get("task_id"),
                "name": item.get("name"),
                "status": item.get("status"),
                "mode": item.get("mode"),
            }
            for item in exact
        ]
        return None, {
            "status": "error",
            "error_code": "ambiguous_task_name",
            "error_type": "AmbiguousTaskName",
            "error": (
                f"task name {identifier!r} matches {len(exact)} tasks; "
                "pass the task_id explicitly"
            ),
            "candidates": candidates,
            "hint": _TASK_NOT_FOUND_HINT,
        }
    return None, None


async def call_with_task_name_fallback(
    svc: _Any,
    identifier: str,
    action: _Callable[[str], _Awaitable[_T]],
) -> tuple[_T | None, dict[str, _Any] | None, str | None]:
    """Run ``action(identifier)``; on task-not-found, retry with name resolution.

    Tools accept ``identifier`` as either a task_id (UUID) or an exact task
    name. The platform service only honours task_id, so this helper plugs
    the gap: it runs ``action`` once with the raw identifier, and if that
    raises :class:`RecordNotFoundError` it consults ``list_tasks_summary``
    for an exact-name match and retries.

    Returns ``(result, error, resolved_from_name)`` where exactly one of
    ``result`` / ``error`` is non-None:

      * ``(result, None, None)`` — primary call succeeded as-is
      * ``(result, None, "<name>")`` — name was resolved and retried
      * ``(None, error_dict, None)`` — ambiguous name, not found, or other
        exception (the caller should return ``error_dict`` verbatim)
    """

    try:
        return await action(identifier), None, None
    except _RecordNotFoundError:
        resolved_id, ambiguous = await _try_resolve_task_id_by_name(svc, identifier)
        if ambiguous is not None:
            return None, ambiguous, None
        if resolved_id is None:
            return None, task_not_found_payload(identifier), None
        try:
            return await action(resolved_id), None, identifier
        except Exception as exc:
            return None, _generic_failure_payload(exc), None
    except Exception as exc:
        return None, _generic_failure_payload(exc), None


# --------------------------------------------------------------------
# Tool result envelope
# --------------------------------------------------------------------


@_dataclass(frozen=True)
class ToolResult:
    """Single-channel tool result: ``text`` is what the model AND the UI
    see. ``is_error`` is an explicit flag so downstream sinks don't have
    to sniff ``[error:...]`` prefixes.

    There is intentionally no separate ``data`` field — anything the
    model needs to act on belongs in ``text`` (prose, formatted list, or
    a fenced JSON block for dense payloads). The frontend's
    ``renderToolResultPayload`` already tries to parse ``text`` as JSON
    and falls back to Markdown, so a single channel feeds both consumers.

    Tools may still return raw ``str``; the registry normalises both
    shapes at the boundary.
    """

    text: str
    is_error: bool = False


class _ToolResultStr(str):
    """``str`` subclass that tags the explicit ``is_error`` flag onto the
    returned string. Keeps every downstream sink that treats results as
    strings (history, persistence, large-result spill-to-disk) working
    unchanged while service-side error detection can read the flag.
    """

    is_error: bool

    def __new__(cls, text: str, *, is_error: bool = False) -> "_ToolResultStr":
        instance = super().__new__(cls, text)
        instance.is_error = is_error
        return instance


ToolExecuteReturn = _Union[str, ToolResult]


def tool_result_from_error_dict(err: dict[str, _Any]) -> ToolResult:
    """Render a structured-error dict as a fully-text :class:`ToolResult`.

    Several helpers in this package return shaped error dicts:
    :func:`wrong_identifier_type_error`, :func:`task_not_found_payload`,
    :func:`_generic_failure_payload`, and the ambiguous-name branch of
    :func:`_try_resolve_task_id_by_name`. The ``error_code`` (or
    ``error_type`` as fallback) becomes the prose prefix token — i.e.
    the skill-contract identifier — and any structured detail
    (``candidates``, ``repair_hints``, ``missing``…) gets rendered as
    follow-on lines so the model sees the same information the dict
    used to carry.
    """

    # Lazy import to keep ``_prose`` free of any dependency on this module
    # (avoids the circular-import landmine when tools at import time pull
    # in helpers from both modules).
    from doyoutrade.tools._prose import format_error_text

    code = str(err.get("error_code") or err.get("error_type") or "tool_error")
    message = str(err.get("error") or err.get("message") or "tool error")
    hint: str | None = None
    raw_hint = err.get("hint")
    if isinstance(raw_hint, str) and raw_hint:
        hint = raw_hint
    else:
        repair = err.get("repair_hints")
        if isinstance(repair, list) and repair:
            hint = "; ".join(str(h) for h in repair)

    text = format_error_text(code, message, hint)

    # Surface structured extras as bullet lists so they survive in text.
    candidates = err.get("candidates")
    if isinstance(candidates, list) and candidates:
        lines = ["Candidates:"]
        for c in candidates:
            if isinstance(c, dict):
                cid = c.get("task_id") or c.get("id") or "?"
                cname = c.get("name", "")
                cstatus = c.get("status", "")
                cmode = c.get("mode", "")
                lines.append(f"- {cid} [{cstatus}] {cname} ({cmode})")
            else:
                lines.append(f"- {c}")
        text += "\n" + "\n".join(lines)

    missing = err.get("missing")
    if isinstance(missing, list) and missing:
        text += f"\nMissing: {', '.join(str(m) for m in missing)}"

    return ToolResult(text=text, is_error=True)


def adapt_sync_dict_to_tool_result(raw: dict[str, _Any]) -> ToolResult:
    """Render a sync tool's plain ``dict`` return as a :class:`ToolResult`.

    The sandboxed file tools in :mod:`doyoutrade.tools.file_tools`
    (``read_strategy_file`` / ``write_strategy_file`` /
    ``edit_strategy_file`` / ``list_strategy_files``) deliberately return
    plain dicts synchronously. The async dispatchers in
    ``doyoutrade.api.app`` and ``doyoutrade.cli._invoke`` use this adapter
    to re-shape them onto the same ``ToolResult.text`` contract async
    tools already publish, so :func:`parse_tool_result` extracts
    ``error_code`` and the data block uniformly.

    Error path (``status == "error"`` or ``is_error`` truthy) reuses
    :func:`tool_result_from_error_dict` to keep the ``[error:<code>]``
    prefix the CLI envelope parser depends on. Success path renders the
    dict as a fenced JSON payload under the ``_summary`` line (when
    present) so the envelope's ``data`` block carries every field.
    """

    from doyoutrade.tools._prose import append_json_payload

    is_error = raw.get("status") == "error" or raw.get("is_error") is True
    if is_error:
        return tool_result_from_error_dict(raw)

    summary_value = raw.get("_summary")
    summary = summary_value if isinstance(summary_value, str) else ""
    payload = {k: v for k, v in raw.items() if k != "_summary"}
    text = append_json_payload(summary, payload) if payload else summary
    return ToolResult(text=text, is_error=False)


# --------------------------------------------------------------------
# OperationHandler and OperationRegistry — moved here from the old tools.py
# --------------------------------------------------------------------


class OperationHandler(ABC):
    name: str = ""
    description: str = ""
    category: str = "agent"
    parameters: dict[str, _Any] = {"type": "object", "properties": {}, "required": []}

    # --- Kwargs-contract opt-in ----------------------------------------
    # Subclasses can declare a map of top-level kwargs that should be
    # auto-lifted into a nested location (e.g. ``settings.universe``)
    # before the rest of the contract runs. Default empty: existing
    # tools keep their current behaviour. See
    # ``doyoutrade.tools._contract.LegacyLift``.
    legacy_top_level_lifts: dict[str, _Any] = {}
    # Subclasses set this True when their schema explicitly allows
    # additional top-level kwargs. Default False matches the
    # ``additionalProperties: False`` posture that ``create_task`` uses.
    accepts_extra_kwargs: bool = False
    # Partial-update tools (e.g. ``update_task``) set this True so a
    # legacy lift that targets ``settings.universe`` still works when
    # the caller did not provide a ``settings`` block at all.
    autocreate_lift_parents: bool = False
    # Declarative input coercion: object/array kwargs that should accept
    # a JSON-string fallback. See
    # ``doyoutrade.tools._coercion.SchemaCoercion``.
    coercion_rules: tuple[_Any, ...] = ()
    # Declarative identifier-kind guards: kwargs that must look like a
    # specific id family. See
    # ``doyoutrade.tools._identifier_kinds.IdentifierGuard``.
    identifier_guards: tuple[_Any, ...] = ()
    # Tools whose full payload must reach the model intact (e.g. SKILL.md
    # bodies returned by ``load_skill``) set this True. The registry then
    # skips the disk-spill, and ``micro_compact_messages`` skips the
    # in-context truncation when it sees the tool's ``name`` on the
    # surfaced ``ToolMessage``.
    bypass_result_truncation: bool = False
    # Tools scoped to "this agent" set this True so the dispatcher auto-fills
    # ``agent_id`` from the calling session's agent. Lets agent-facing tools
    # like ``create_cron_job`` accept the agent_id as optional and default it
    # to the caller, instead of forcing the model to recite its own id.
    requires_calling_agent_id: bool = False
    # Tools that push messages back to the user's session (e.g. cron job
    # creation, where the cron-fire reply should land in the chat the user
    # is sitting in) set this True. The dispatcher then auto-fills
    # ``target_session_id`` from the calling session, so the model doesn't
    # need to know — and can't mistype — its own session id.
    requires_calling_session_id: bool = False

    def to_openai_schema(self) -> dict[str, _Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @_abstractmethod
    async def execute(self, **kwargs: _Any) -> ToolExecuteReturn:
        raise NotImplementedError

    # --- Contract helpers ---------------------------------------------
    def _allowed_top_level_kwargs(self) -> frozenset[str]:
        """Default allowlist: top-level keys declared on ``cls.parameters``."""

        props = self.parameters.get("properties", {}) if isinstance(self.parameters, dict) else {}
        if not isinstance(props, dict):
            return frozenset()
        return frozenset(props.keys())

    def _suggested_kwarg_paths(self) -> dict[str, str]:
        """Override to map mistakenly-top-level kwargs to their canonical path.

        Default implementation derives ``settings.<key>`` for every key
        declared inside ``parameters.properties.settings.properties``.
        Tools that nest under a different parent should override.
        """

        if not isinstance(self.parameters, dict):
            return {}
        props = self.parameters.get("properties", {})
        if not isinstance(props, dict):
            return {}
        settings_schema = props.get("settings")
        if not isinstance(settings_schema, dict):
            return {}
        nested = settings_schema.get("properties")
        if not isinstance(nested, dict):
            return {}
        return {key: f"settings.{key}" for key in nested.keys()}

    def _enforce_kwargs_contract(self, kwargs: dict[str, _Any]) -> _Any:
        """Apply the kwargs contract using this tool's declared metadata."""

        from doyoutrade.tools._contract import (
            enforce_kwargs_contract as _enforce,
        )

        return _enforce(
            kwargs,
            allowed_top_level=self._allowed_top_level_kwargs(),
            suggested_paths=self._suggested_kwarg_paths(),
            legacy_lifts=self.legacy_top_level_lifts,
            autocreate_missing_parents=self.autocreate_lift_parents,
        )

    def _apply_schema_coercion(self, kwargs: dict[str, _Any]) -> _Any:
        """Apply this tool's declared schema-coercion rules to ``kwargs``."""

        from doyoutrade.tools._coercion import (
            apply_schema_coercion as _coerce,
        )

        return _coerce(kwargs, self.coercion_rules)

    def _apply_identifier_guards(self, kwargs: dict[str, _Any]) -> dict[str, _Any] | None:
        """Run this tool's declared identifier-kind guards. Returns
        the first error dict (already shaped like the canonical
        ``status: error`` payload) or ``None`` when all kwargs match
        their declared kind."""

        from doyoutrade.tools._identifier_kinds import (
            apply_identifier_guards as _guard,
        )

        return _guard(kwargs, self.identifier_guards)


class OperationRegistry:
    def __init__(
        self,
        tools: list[OperationHandler] | None = None,
        tool_result_max_chars: int = 50000,
    ):
        self._tools = {tool.name: tool for tool in (tools or [])}
        self._tool_result_max_chars = tool_result_max_chars

    def definitions(self) -> list[dict[str, _Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def list_tools(self) -> list[dict[str, _Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "category": getattr(tool, "category", "agent"),
            }
            for tool in self._tools.values()
        ]

    def get(self, name: str) -> OperationHandler | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    async def execute(
        self,
        name: str,
        args: dict[str, _Any],
        session_id: str | None = None,
        calling_agent_id: str | None = None,
    ) -> str:
        tool = self.get(name)
        if tool is None:
            return _ToolResultStr(
                f"[error:tool_not_found] tool not found: {name}",
                is_error=True,
            )
        call_args = dict(args or {})
        if session_id and getattr(tool, "requires_session_id", False):
            call_args.setdefault("session_id", session_id)
        if (
            calling_agent_id
            and getattr(tool, "requires_calling_agent_id", False)
            and not call_args.get("agent_id")
        ):
            call_args["agent_id"] = calling_agent_id
        if (
            session_id
            and getattr(tool, "requires_calling_session_id", False)
            and not call_args.get("target_session_id")
        ):
            call_args["target_session_id"] = session_id
        try:
            raw: _Any = tool.execute(**call_args)
            if _inspect_isawaitable(raw):
                raw = await raw
            # Sync dict-returning tools (file primitives) need the same
            # adapt step that the CLI dispatcher uses.
            if isinstance(raw, dict):
                raw = adapt_sync_dict_to_tool_result(raw)
        except Exception as exc:
            metadata = _exception_metadata(exc)
            # Plain prose: everything we want the model to see is flat-scalar,
            # so an embedded JSON block would just be noise. The traceback tail
            # is short enough to inline.
            text = (
                f"[error:{type(exc).__name__}] {metadata['error']}"
                f"\nTool: {name}"
                f"\nTraceback: {metadata['traceback_tail']}"
            )
            return _ToolResultStr(text, is_error=True)

        if isinstance(raw, ToolResult):
            text = raw.text
            is_error = raw.is_error
        else:
            text = raw if isinstance(raw, str) else str(raw)
            is_error = bool(getattr(raw, "is_error", False))

        # Spill oversized text to disk so the model gets a preview + filepath
        # rather than a multi-MB blob in conversation history. This is the
        # one and only size guard now that there's no separate data channel
        # — info-retrieval tools that embed a JSON block in ``text`` ride
        # this same code path. Tools that opt in to ``bypass_result_truncation``
        # (e.g. ``load_skill``) skip this guard so their full payload reaches
        # the model.
        if (
            session_id
            and len(text) > self._tool_result_max_chars
            and not getattr(tool, "bypass_result_truncation", False)
        ):
            storage = ToolResultStorage(session_id)
            try:
                key = f"{name}_{id(text)}"
                filepath, preview = await storage.persist(key, text)
                text = storage.build_preview_message(key, len(text), preview, filepath)
            except Exception:
                pass

        if is_error:
            return _ToolResultStr(text, is_error=is_error)
        return text

    async def aclose(self) -> None:
        for tool in self._tools.values():
            close = getattr(tool, "aclose", None)
            if close is not None:
                await close()


# --------------------------------------------------------------------
# Built-in tool implementations — kept inline to preserve existing behaviour
# --------------------------------------------------------------------


class LoadSkillTool(OperationHandler):
    name = "load_skill"
    description = "Load the full SKILL.md instructions for a doyoutrade skill by name."
    category = "agent"
    # SKILL.md bodies must reach the model intact — both the registry's
    # disk-spill and the in-context micro-compaction skip this tool.
    bypass_result_truncation = True
    # Opt into the calling session_id auto-fill (same mechanism the bash
    # tools use). We need it so the body delivered to the model this turn
    # can also be persisted to ``assistant_loaded_skills``; the persisted
    # row is what the reminder builder (T3) replays as a
    # ``<system-reminder>`` after context compaction folds the original
    # tool_result block away. The model must NOT have to recite its own
    # session id — that would let it lie and break the persistence key.
    requires_session_id = True
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill name, e.g. technical-basic or backtest-diagnose.",
            }
        },
        "required": ["skill_name"],
    }

    def __init__(
        self,
        loaded_skill_repository: _Any | None = None,
        assistant_repository: _Any | None = None,
    ) -> None:
        # Optional dependency: when wired (via ``build_default_tool_registry``)
        # we persist the loaded SKILL.md body so it survives compaction.
        # Tests / CLI-style invocations can construct without the repo and
        # still get the "deliver this turn" behaviour; the persistence step
        # is a no-op when the repo is absent OR session_id is None.
        self._loaded_skill_repository = loaded_skill_repository
        # Needed only for flow skills: starting a flow writes
        # ``session.config["active_flow"]`` via ``update_session_config``.
        # Loading a flow skill without this wiring is a hard error (the
        # flow engine would never engage), not a silent downgrade.
        self._assistant_repository = assistant_repository

    async def execute(  # type: ignore[override]
        self,
        skill_name: str,
        session_id: str | None = None,
    ) -> ToolResult:
        # Override uses named args (same convention as other concrete tools
        # like CompactTool below); base OperationHandler.execute declares
        # **kwargs, which Pyright flags as incompatible. Weakening to
        # **kwargs would silently swallow unknown args — the failure mode
        # CLAUDE.md §错误可见性 explicitly forbids — so suppress the strict
        # override warning instead.
        import hashlib
        import logging

        from doyoutrade.debug import emit_debug_event
        from doyoutrade.tools._prose import format_error_text

        logger = logging.getLogger(__name__)

        target = skill_name.strip()
        for skill in load_skills(enabled_only=True):
            if skill.name == target or skill.skill_path == target:
                body_bytes = skill.body.encode("utf-8")
                body_hash = hashlib.sha256(body_bytes).hexdigest()

                # Flow skills are validated up front: a malformed flowchart
                # or missing runtime wiring must fail THIS call with a
                # structured error, never load as a plain skill (the model
                # would follow prose while the engine never engages).
                flow = None
                if skill.skill_type == "flow":
                    from doyoutrade.skills.flow import (
                        FlowError,
                        extract_flow_from_skill_body,
                    )

                    if not session_id or self._assistant_repository is None:
                        await emit_debug_event(
                            "operation_load_skill.failed",
                            {
                                "session_id": session_id,
                                "skill_name": skill.name,
                                "error_code": "flow_runtime_unwired",
                                "hint": (
                                    "flow skills need the assistant session "
                                    "repository wired into LoadSkillTool "
                                    "(build_default_tool_registry assistant_repository=...)"
                                ),
                            },
                        )
                        return ToolResult(
                            text=format_error_text(
                                "flow_runtime_unwired",
                                f"skill {skill.name!r} is a flow skill but this "
                                "runtime has no session-state wiring; it cannot "
                                "be executed here.",
                            ),
                            is_error=True,
                        )
                    try:
                        flow = extract_flow_from_skill_body(skill.body)
                    except FlowError as exc:
                        logger.error(
                            "load_skill.flow_parse_error skill_name=%s err=%s: %s",
                            skill.name,
                            type(exc).__name__,
                            exc,
                        )
                        await emit_debug_event(
                            "operation_load_skill.failed",
                            {
                                "session_id": session_id,
                                "skill_name": skill.name,
                                "error_code": "flow_parse_error",
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                                "hint": (
                                    "fix the mermaid flowchart in SKILL.md; see "
                                    "doyoutrade/skills/flow.py for the supported subset"
                                ),
                            },
                        )
                        return ToolResult(
                            text=format_error_text(
                                "flow_parse_error",
                                f"flow skill {skill.name!r} has an invalid flowchart: {exc}",
                            ),
                            is_error=True,
                        )

                # Idempotent short-circuit (PR-D): if this exact body
                # (matching ``body_hash``) is already persisted for this
                # session, the agent has already received the SKILL.md in
                # an earlier turn — either still visible in conversation
                # history, or, after context compaction, replayed as a
                # ``<system-reminder>`` by ``loaded_skills_reminder``.
                # Returning a small stub instead of the full body (~5K
                # tokens for strategy-definition-authoring) makes
                # PR-B's "MUST load_skill before SDK code" rule cheap to
                # satisfy when nothing has actually changed on disk; the
                # body_hash check is what makes that safe — an on-disk
                # edit produces a mismatched hash and we fall through to
                # the full reload + upsert below.
                #
                # The repository read is best-effort: a transient DB
                # failure here logs a warning and falls through to the
                # full path so the session is never blocked on a degraded
                # read.  We do NOT update ``loaded_at`` on a short-circuit
                # — semantically the row is unchanged and we want
                # ``loaded_at`` to reflect the first time the body was
                # delivered, which keeps the reminder's
                # "newest-skill-first" sort meaningful.
                # Flow skills skip the short-circuit on purpose: re-loading a
                # flow skill restarts the flow at its first node (explicit
                # restart semantics, surfaced via flow_started below).
                if skill.skill_type != "flow" and session_id and self._loaded_skill_repository is not None:
                    try:
                        existing_rows = (
                            await self._loaded_skill_repository.list_by_session(session_id)
                        )
                    except Exception as exc:
                        logger.warning(
                            "load_skill.shortcircuit_check_failed "
                            "session_id=%s skill_name=%s err=%s: %s",
                            session_id,
                            skill.name,
                            type(exc).__name__,
                            exc,
                        )
                        existing_rows = []
                    for row in existing_rows:
                        if (
                            row.get("skill_name") == skill.name
                            and row.get("body_hash") == body_hash
                        ):
                            await emit_debug_event(
                                "operation_load_skill.shortcircuit",
                                {
                                    "session_id": session_id,
                                    "skill_name": skill.name,
                                    "loaded_at": str(row.get("loaded_at")),
                                    "body_hash": body_hash[:16],
                                    "skill_path": str(skill.skill_path),
                                    "saved_bytes": len(body_bytes),
                                },
                            )
                            stub_text = (
                                f"Skill {skill.name!r} was already loaded in "
                                f"this session at {row.get('loaded_at')} and "
                                f"the SKILL.md on disk is unchanged (body "
                                f"hash matches). The full body remains in "
                                f"earlier conversation history; after any "
                                f"context compaction the assistant runtime "
                                f"re-injects it as a `<system-reminder>` "
                                f"automatically. Do not invoke load_skill "
                                f"again for {skill.name!r} in this session "
                                f"unless the SKILL.md file has actually been "
                                f"edited on disk."
                            )
                            return ToolResult(text=stub_text)

                base_text = (
                    f"Loaded skill {skill.name!r} from {skill.skill_path}.\n"
                    f"Base directory: {skill.skill_dir}\n"
                    "Resolve relative paths in SKILL.md (e.g. "
                    "`references/foo.md`, `scripts/bar.py`) against this base "
                    "directory; read them with the `read_file` tool.\n\n"
                    f"--- SKILL.md ---\n{skill.body}"
                )

                # Persistence is best-effort but its outcome shapes the
                # tail-note: only promise compaction survival when we
                # actually wrote the row. ``session_id`` being None is a
                # legitimate path (CLI / non-assistant invocations), not
                # an error.
                persisted = False
                if session_id and self._loaded_skill_repository is not None:
                    try:
                        await self._loaded_skill_repository.upsert(
                            session_id=session_id,
                            skill_name=skill.name,
                            skill_path=str(skill.skill_path),
                            body=skill.body,
                            body_hash=body_hash,
                            metadata={"source": "load_skill_tool"},
                        )
                    except Exception as exc:
                        # Per CLAUDE.md §错误可见性: structured log + debug
                        # event with error type/message + hint; do NOT
                        # swallow silently. We still return the SKILL.md
                        # to the model so the current turn works — only
                        # the compaction-survival promise is lost.
                        logger.warning(
                            "load_skill.persistence_failed session_id=%s "
                            "skill_name=%s err=%s: %s",
                            session_id,
                            skill.name,
                            type(exc).__name__,
                            exc,
                        )
                        await emit_debug_event(
                            "operation_load_skill.persistence_failed",
                            {
                                "session_id": session_id,
                                "skill_name": skill.name,
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                                "hint": (
                                    "skill content delivered for this turn "
                                    "but will not survive compaction; check "
                                    "assistant_loaded_skills table or DB "
                                    "connectivity"
                                ),
                            },
                        )
                    else:
                        persisted = True
                        await emit_debug_event(
                            "operation_load_skill.persisted",
                            {
                                "session_id": session_id,
                                "skill_name": skill.name,
                                "byte_size": len(body_bytes),
                                "body_hash": body_hash[:16],
                            },
                        )

                if persisted:
                    base_text = (
                        base_text
                        + "\n\n[Loaded and persisted; will be re-injected as "
                        "<system-reminder> after context compaction. Do not "
                        "invoke load_skill again for the same skill in this "
                        "session unless the SKILL.md content has changed on "
                        "disk.]"
                    )

                if flow is not None:
                    from datetime import datetime, timezone

                    from doyoutrade.skills.flow import (
                        ABORT_CHOICE,
                        TASK_ADVANCE_CHOICE,
                    )

                    entry_id = flow.entry_node_id()
                    restarted = False
                    try:
                        current = await self._assistant_repository.get_session(session_id)
                        previous_flow = (
                            (current or {}).get("config") or {}
                        ).get("active_flow")
                        restarted = bool(previous_flow)
                        await self._assistant_repository.update_session_config(
                            session_id,
                            {
                                "active_flow": {
                                    "skill_name": skill.name,
                                    "node_id": entry_id,
                                    "invalid_choice": None,
                                    "started_at": datetime.now(timezone.utc).isoformat(),
                                }
                            },
                        )
                    except Exception as exc:
                        # The flow cannot run without persisted state — this
                        # is a hard failure of the call, not a downgrade.
                        logger.error(
                            "load_skill.flow_state_init_failed session_id=%s "
                            "skill_name=%s err=%s: %s",
                            session_id,
                            skill.name,
                            type(exc).__name__,
                            exc,
                        )
                        await emit_debug_event(
                            "operation_load_skill.failed",
                            {
                                "session_id": session_id,
                                "skill_name": skill.name,
                                "error_code": "flow_state_init_failed",
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                                "hint": "session config write failed; check assistant_sessions table",
                            },
                        )
                        return ToolResult(
                            text=format_error_text(
                                "flow_state_init_failed",
                                f"could not persist flow state for {skill.name!r}: {exc}",
                            ),
                            is_error=True,
                        )
                    await emit_debug_event(
                        "operation_load_skill.flow_started",
                        {
                            "session_id": session_id,
                            "skill_name": skill.name,
                            "entry_node_id": entry_id,
                            "node_count": len(flow.nodes),
                            "restarted": restarted,
                        },
                    )
                    entry_label = flow.nodes[entry_id].label
                    base_text = (
                        base_text
                        + "\n\n[Flow skill engaged"
                        + (" (restarted from the first step)" if restarted else "")
                        + f": {len(flow.nodes)} nodes. Current step: {entry_label!r}. "
                        "Each turn a <system-reminder> describes the current "
                        "step and its branches. Advance by ending a reply with "
                        f"<choice>{TASK_ADVANCE_CHOICE}</choice> (task steps) or "
                        "<choice>branch label</choice> (decision steps); abandon "
                        f"with <choice>{ABORT_CHOICE}</choice>.]"
                    )
                return ToolResult(text=base_text)
        return ToolResult(
            text=format_error_text("skill_not_found", f"skill not found: {target}"),
            is_error=True,
        )


class CompactTool(OperationHandler):
    name = "compact"
    description = "Request a concise summary of the conversation context before continuing."
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "focus": {"type": "string", "description": "What the compact summary should preserve."}
        },
        "required": [],
    }

    async def execute(self, focus: str = "") -> ToolResult:
        instruction = (
            "Summarize prior context into goals, constraints, tool findings, and next steps."
        )
        text = instruction if not focus else f"{instruction}\nFocus: {focus}"
        return ToolResult(text=text)


async def _safe_lookup_existing_runs(
    platform_service: _Any | None,
    task_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, _Any]]:
    """Best-effort fetch of recent backtest run summaries for a task.

    Returns the raw ``list_backtest_jobs`` item dicts so callers can branch on
    ``status`` (running / completed / failed) rather than just receiving a list
    of ids. Used by ``RunStrategyBacktestTool`` to dispatch existing-run
    handling.
    """

    if platform_service is None or not hasattr(platform_service, "list_backtest_jobs"):
        return []
    try:
        result = await platform_service.list_backtest_jobs(task_id, limit=limit)
    except Exception:
        return []
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    return [entry for entry in items if isinstance(entry, dict) and entry.get("run_id")]


async def _safe_lookup_existing_run_ids(
    platform_service: _Any | None,
    task_id: str,
    *,
    limit: int = 5,
) -> list[str]:
    """Backwards-compatible thin wrapper over ``_safe_lookup_existing_runs``."""

    runs = await _safe_lookup_existing_runs(platform_service, task_id, limit=limit)
    return [rid for rid in (str(r.get("run_id") or "") for r in runs) if rid]


async def _safe_auto_clone_task(
    platform_service: _Any | None,
    task_id: str,
) -> str | None:
    """Best-effort clone of a one-shot backtest task.

    Returns the cloned task's id when the platform service exposes
    ``clone_task`` and the call succeeds, or ``None`` otherwise. Used to
    enrich the ``backtest_run_already_exists`` error payload so the agent
    can retry the backtest in a single turn instead of having to call
    ``clone_task`` manually.
    """

    if platform_service is None or not hasattr(platform_service, "clone_task"):
        return None
    try:
        cloned = await platform_service.clone_task(task_id)
    except Exception:
        return None
    cloned_task_id = getattr(cloned, "task_id", None)
    if isinstance(cloned_task_id, str) and cloned_task_id:
        return cloned_task_id
    if isinstance(cloned, dict):
        candidate = cloned.get("task_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def build_default_tool_registry(
    *,
    tool_result_max_chars: int = 50000,
    loaded_skill_repository: _Any | None = None,
    assistant_repository: _Any | None = None,
    job_watch_repository: _Any | None = None,
    run_repository: _Any | None = None,
    decision_signal_repository: _Any | None = None,
    instrument_catalog_repository: _Any | None = None,
    knowledge_graph_repository: _Any | None = None,
    model_adapter_factory: _Any | None = None,
) -> OperationRegistry:
    """Return the agent's tool registry.

    Architectural rule (effective 2026-05-23, updated 2026-05-25): the
    agent sees two categories of tools.

    **Framework primitives** — tools with no domain dependency:
      ``load_skill``, ``compact``, ``execute_bash``, ``manage_bash_tasks``.

    **File primitives** (``read_file`` / ``write_file`` / ``edit_file``
    / ``list_files``) — a second category alongside the framework
    primitives.  ``read_file`` is unrestricted (reads any path); the
    mutation tools (``write_file`` / ``edit_file``) enforce the
    ``_sandbox`` module-level registry for scope enforcement.  Lifecycle
    / domain operations (open / cancel / compile / finalize) still go
    via CLI.

    **All other domain operations** (task / cron / strategy / backtest /
    cycle / model route / market data / factor / pattern / stock / sdk
    discovery / strategy validation) are exposed via ``doyoutrade-cli``
    only — the agent reaches them by shelling out through
    ``execute_bash``.  This keeps the agent's visible tool surface
    minimal and forces every domain call through the same CLI contract.

    Cron writes specifically go CLI → API server HTTP because the
    in-process ``AgentCronManager`` (and its APScheduler) is the
    authoritative scheduler — see ``doyoutrade/cli/_api.py``.

    The domain ``*Tool`` classes still live in ``doyoutrade/tools/`` and
    ``doyoutrade/assistant/strategy_tools/`` because the CLI commands
    instantiate them directly as their implementation layer. They are
    intentionally **not** re-exported from this package — anyone reading
    ``from doyoutrade.tools import ...`` should see only the agent
    surface, not the CLI's internal building blocks.
    """
    from doyoutrade.tools.file_tools import (
        ReadFileTool as _ReadFileTool,
        WriteFileTool as _WriteFileTool,
        EditFileTool as _EditFileTool,
        ListFilesTool as _ListFilesTool,
    )
    from doyoutrade.tools.knowledge_index import KnowledgeIndexTool as _KnowledgeIndexTool
    from doyoutrade.tools.knowledge_graph import KnowledgeGraphTool as _KnowledgeGraphTool
    from doyoutrade.tools._sandbox import register_knowledge_sandbox

    # Standing writable area: ``~/.doyoutrade/knowledge`` is permanently
    # registered so ``write_file`` / ``edit_file`` accept it (the agent writes
    # there only when the user explicitly asks — gating is behavioural, see the
    # main-agent prompt + the ``doyoutrade-knowledge`` skill). Strategy-authoring
    # ``work_dir`` roots are still registered / unregistered per lifecycle.
    register_knowledge_sandbox()

    bash_task_manager = BashTaskManager()
    from doyoutrade.tools.ask_user import AskUserQuestionTool as _AskUserQuestionTool
    from doyoutrade.tools.watch_job import WatchJobTool as _WatchJobTool
    from doyoutrade.tools.decision_signal import (
        RecordDecisionSignalTool as _RecordDecisionSignalTool,
    )
    from doyoutrade.tools.portfolio_import import (
        ImportPositionsFromImageTool as _ImportPositionsFromImageTool,
        ImportTradesCsvTool as _ImportTradesCsvTool,
    )

    tools = [
        LoadSkillTool(
            loaded_skill_repository=loaded_skill_repository,
            assistant_repository=assistant_repository,
        ),
        _AskUserQuestionTool(assistant_repository=assistant_repository),
        _WatchJobTool(
            watch_repository=job_watch_repository,
            run_repository=run_repository,
        ),
        # Exceptions to the "domain ops go via CLI" rule above: these two
        # need in-process wiring the CLI contract cannot carry —
        # session-identity attribution (record_decision_signal) and a live
        # multimodal model adapter (import_positions_from_image). The CSV
        # variant rides along so both import entrances share one tool
        # surface; unwired runtimes fail structurally, never silently.
        _RecordDecisionSignalTool(
            decision_signal_repository=decision_signal_repository,
        ),
        _ImportPositionsFromImageTool(
            model_adapter_factory=model_adapter_factory,
            instrument_catalog_repository=instrument_catalog_repository,
        ),
        _ImportTradesCsvTool(),
        CompactTool(),
        _ReadFileTool(),
        _WriteFileTool(),
        _EditFileTool(),
        _ListFilesTool(),
        _KnowledgeIndexTool(),
        # 图谱与索引同属知识库检索面：index 是文件导航地图，graph 是实体
        # 关系层（bi-temporal 事实）。repository 未装配的 runtime 里工具仍
        # 注册，调用时返回结构化 knowledge_graph_unwired（不静默消失）。
        _KnowledgeGraphTool(knowledge_graph_repository=knowledge_graph_repository),
        ExecuteBashTool(task_manager=bash_task_manager),
        ManageBashTasksTool(task_manager=bash_task_manager),
    ]
    return OperationRegistry(tools, tool_result_max_chars=tool_result_max_chars)


def resolve_tool_registry_factory() -> _Callable[..., OperationRegistry]:
    """Return the tool-registry factory the runtime should assemble tools with.

    ``DOYOUTRADE_TOOL_REGISTRY_FACTORY`` (format ``package.module:callable``)
    lets a deployment layer (e.g. a cloud profile) swap the agent's tool
    surface without forking: the named callable must accept the same keyword
    arguments as :func:`build_default_tool_registry` and return an
    :class:`OperationRegistry`. Unset/blank → the built-in default.

    Resolution failures **raise** (never fall back silently): a deployment
    that configured a factory expects the restricted/customized surface, and
    silently serving the default tool set instead would undo exactly the
    gating the factory exists to apply.
    """
    spec = (_os.environ.get("DOYOUTRADE_TOOL_REGISTRY_FACTORY") or "").strip()
    if not spec:
        return build_default_tool_registry
    module_name, sep, attr_name = spec.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError(
            "DOYOUTRADE_TOOL_REGISTRY_FACTORY must be 'package.module:callable', "
            f"got {spec!r}"
        )
    import importlib

    try:
        module = importlib.import_module(module_name.strip())
    except ImportError as exc:
        raise ImportError(
            f"DOYOUTRADE_TOOL_REGISTRY_FACTORY module {module_name.strip()!r} "
            f"could not be imported: {exc}"
        ) from exc
    try:
        factory = getattr(module, attr_name.strip())
    except AttributeError as exc:
        raise AttributeError(
            f"DOYOUTRADE_TOOL_REGISTRY_FACTORY attribute {attr_name.strip()!r} "
            f"not found in module {module_name.strip()!r}"
        ) from exc
    if not callable(factory):
        raise TypeError(
            f"DOYOUTRADE_TOOL_REGISTRY_FACTORY target {spec!r} resolved to "
            f"non-callable {type(factory).__name__}"
        )
    return factory


from doyoutrade.tools.bash import (
    BashTaskManager,
    ExecuteBashTool,
    ManageBashTasksTool,
)

__all__ = [
    "OperationHandler",
    "OperationRegistry",
    "build_default_tool_registry",
    "resolve_tool_registry_factory",
    "LoadSkillTool",
    "CompactTool",
    "BashTaskManager",
    "ExecuteBashTool",
    "ManageBashTasksTool",
]
