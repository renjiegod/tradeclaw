"""Code-level definition of the fixed builtin main agent.

The "默认智能体" used to be a plain DB row that an operator could rename, delete,
or have its prompt / skills / tools edited freely. It is now a **code-fixed main
agent**: its *identity and capabilities* are owned by code and re-pinned on every
boot, while only a few runtime knobs stay user-editable.

Single source of truth for:

- the well-known id / name (``MAIN_AGENT_ID`` / ``MAIN_AGENT_NAME``),
- the locked system prompt — linked to the authoritative ``main_agent.j2`` via
  ``MAIN_AGENT_PROMPT_TEMPLATE_ID`` (CLAUDE.md calls that file the authoritative
  main-agent prompt),
- the code-controlled skills (every enabled skill in the skills directory) and
  tools (the full in-process tool registry),
- which fields a user may still edit (``MAIN_AGENT_EDITABLE_FIELDS``).

Design split (see plan): the cheap, static identity fields are injected by the
repository serializer (``apply_main_agent_overrides``) so API / frontend / runtime
all agree; the *dynamic* "all skills / all tools" expansion happens at the two
service load points (``_resolve_tool_inventory`` uses the live registry;
``_compose_effective_system_prompt`` calls ``load_skills``) so the hot
``list_agents`` path never pays a directory scan. ``builtin_skill_names`` /
``builtin_tool_names`` here are only for the rarer clone path, which materializes
concrete lists into the new editable copy.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

from doyoutrade.assistant.signal_composer_agent import (
    SIGNAL_COMPOSER_AGENT_ID,
    SIGNAL_COMPOSER_AGENT_NAME,
    SIGNAL_COMPOSER_EDITABLE_FIELDS,
    SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
    is_signal_composer_agent,
)

# Well-known id of the fixed main agent. Kept as the historical "agent_default"
# so existing channel FKs / sessions / deployments keep resolving; centralized
# here and referenced by ChannelManager / trigger resolution / repository guards.
MAIN_AGENT_ID = "agent_default"
MAIN_AGENT_NAME = "默认智能体"

# Locks the system prompt to the authoritative on-disk template (main_agent.j2).
MAIN_AGENT_PROMPT_TEMPLATE_ID = "main-agent"

# The only fields a user may edit on the fixed main agent. Everything else
# (name / status / system_prompt / template / skills / tools) is code-controlled.
MAIN_AGENT_EDITABLE_FIELDS: tuple[str, ...] = (
    "model_route_name",
    "context_compaction",
    "max_turns",
)

# Advertised editable surface for ordinary custom agents (full CRUD). Surfaced in
# the serialized payload so the frontend can render restrictions generically.
CUSTOM_AGENT_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "status",
    "system_prompt",
    "system_prompt_template_id",
    "model_route_name",
    "tool_configs",
    "skill_names",
    "max_turns",
    "context_compaction",
)


def is_main_agent(agent: str | Mapping[str, Any] | None) -> bool:
    """True for the fixed main agent, by id or by the legacy ``is_default`` marker.

    Accepts either an agent id string or a (serialized) agent mapping. With a
    second builtin now in play (the signal-card composer, also
    ``is_builtin=True``), the bare ``is_builtin`` flag no longer uniquely
    identifies the *main* agent — so dict detection keys on the id, with the
    legacy ``is_default=True`` marker kept as a pre-migration fallback (the
    is_builtin migration backfilled the main agent row, which has always been
    ``is_default``).
    """
    if agent is None:
        return False
    if isinstance(agent, str):
        return agent == MAIN_AGENT_ID
    return agent.get("id") == MAIN_AGENT_ID or (
        bool(agent.get("is_default")) and agent.get("id") != SIGNAL_COMPOSER_AGENT_ID
    )


def builtin_skill_names() -> list[str]:
    """Every *enabled* skill name in the skills directory (clone-path use).

    Uncached so a fresh clone reflects the current directory; lazy import avoids
    pulling the skills loader into module import time.
    """
    from doyoutrade.skills.loader import load_skills

    return [s.name for s in load_skills(enabled_only=True)]


@lru_cache(maxsize=1)
def builtin_tool_names() -> tuple[str, ...]:
    """The full in-process tool registry names (clone-path use).

    Cached: the tool set is static per process. Lazy import avoids a heavy /
    potentially circular import at module load.
    """
    from doyoutrade.tools import build_default_tool_registry

    return tuple(build_default_tool_registry().names)


def is_builtin_agent(agent: str | Mapping[str, Any] | None) -> bool:
    """True for ANY code-fixed builtin agent (main agent OR signal composer).

    Broader than :func:`is_main_agent`: both builtins are undeletable and have
    code-controlled identity, so delete / lock guards key on this. The
    per-builtin editable surface is resolved by :func:`builtin_agent_identity`.
    """
    return is_main_agent(agent) or is_signal_composer_agent(agent)


def builtin_agent_identity(
    agent: str | Mapping[str, Any] | None
) -> tuple[str, str, str, tuple[str, ...]] | None:
    """Return ``(id, name, prompt_template_id, editable_fields)`` for a builtin.

    Dispatches by id; returns ``None`` for a custom (non-builtin) agent so the
    serializer/guards can fall through to the full-editable custom path. Single
    source of truth for what each code-fixed builtin pins, so adding a third
    builtin only touches this table.
    """
    if is_main_agent(agent):
        return (
            MAIN_AGENT_ID,
            MAIN_AGENT_NAME,
            MAIN_AGENT_PROMPT_TEMPLATE_ID,
            MAIN_AGENT_EDITABLE_FIELDS,
        )
    if is_signal_composer_agent(agent):
        return (
            SIGNAL_COMPOSER_AGENT_ID,
            SIGNAL_COMPOSER_AGENT_NAME,
            SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
            SIGNAL_COMPOSER_EDITABLE_FIELDS,
        )
    return None


def apply_main_agent_overrides(row: dict[str, Any]) -> dict[str, Any]:
    """Inject code-controlled static identity onto a serialized agent dict.

    Mutates and returns ``row`` in place. For ANY builtin agent (main agent OR
    signal-card composer) this re-pins the locked identity
    (name / prompt template / status / ``is_builtin`` flag) and reports the
    locked editable surface; it deliberately does NOT expand skills/tools here
    (the service load points do that on session start, off the hot path — and
    the composer agent deliberately carries NONE). For a custom agent it just
    annotates ``is_builtin=False`` plus the full editable surface so the
    frontend can render generically.

    Always preserves the user-editable knobs (``model_route_name`` /
    ``context_compaction`` / ``max_turns``) and timestamps already on ``row``.
    """
    if not isinstance(row, dict):
        return row
    identity = builtin_agent_identity(row)
    if identity is not None:
        _id, name, template_id, editable = identity
        row["is_builtin"] = True
        row["name"] = name
        row["status"] = "active"
        row["system_prompt_template_id"] = template_id
        row["prompt_template_id"] = template_id
        # Both builtins render their locked template; the free-form
        # ``system_prompt`` field is never the source of truth, so blank it to
        # keep the serialized row honest (resolve_agent_system_prompt picks the
        # template when template_id is set).
        row["system_prompt"] = ""
        row["editable_fields"] = list(editable)
    else:
        row.setdefault("is_builtin", False)
        row["is_builtin"] = bool(row.get("is_builtin"))
        row["editable_fields"] = list(CUSTOM_AGENT_EDITABLE_FIELDS)
    return row
