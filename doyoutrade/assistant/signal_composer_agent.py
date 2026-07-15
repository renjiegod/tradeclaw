"""Code-level definition of the fixed builtin signal-card composer agent.

Companion to :mod:`doyoutrade.assistant.main_agent`. Where the main agent owns
the full operator surface (every tool, every skill, the authoritative
``main_agent.j2`` system prompt), this agent owns **one** narrow job: turning a
fired Trigger's cycle digest into a single push-card message body.

Why a dedicated agent (instead of reusing the main agent for prose pushes):

- **Noise reduction.** The prose compose turn used to run on the main agent,
  which carries the entire CLI / cron / skill / tool surface in its system
  prompt. That surface is irrelevant to "narrate this digest in Chinese" and
  bloats every compose call's context — and, worse, it left the model free to
  invent a different card title / layout on every fire (no fixed template).
  This agent carries NO tools and NO skills; its system prompt
  (``signal_card_composer.j2``) is a few lines focused purely on digest → card.
- **Fixed output shape.** Compose-only is now enforced structurally: zero
  tools means there is nothing to call, and the framing template
  (``trigger_digest_framing.j2``) pins the title line + section skeleton so the
  title is deterministic across fires (only the section *contents* are LLM
  narrated).

Single source of truth for:

- the well-known id / name (``SIGNAL_COMPOSER_AGENT_ID`` / ``SIGNAL_COMPOSER_AGENT_NAME``),
- the locked system prompt — linked to ``signal_card_composer.j2`` via
  ``SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID``,
- zero tools / zero skills (the row stores empty lists; nothing is expanded at
  runtime — see ``AssistantService._resolve_tool_inventory`` which only
  short-circuits the *main* agent to the full registry),
- which fields a user may still edit (``SIGNAL_COMPOSER_EDITABLE_FIELDS``).

Seeded on every boot by
:meth:`SqlAlchemyAgentRepository.ensure_signal_composer_agent` and made the
default composer for prose trigger delivery in
:mod:`doyoutrade.runtime.trigger_delivery`.
"""

from __future__ import annotations

from typing import Any, Mapping

# Well-known id of the fixed signal-card composer agent. Stable so trigger
# ``delivery_json.composer_agent_id`` / cron references / sessions keep
# resolving across boots. Distinct from MAIN_AGENT_ID — this agent never
# carries tools/skills.
SIGNAL_COMPOSER_AGENT_ID = "agent_signal_composer"
SIGNAL_COMPOSER_AGENT_NAME = "信号卡片撰写器"

# Locks the system prompt to the lean on-disk template (signal_card_composer.j2).
SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID = "signal-card-composer"

# The only fields a user may edit on the fixed composer agent. Everything else
# (name / status / system prompt / template / tools / skills) is code-controlled.
# Mirrors the main agent's editable surface — a fixed builtin exposes only the
# runtime knobs, never its identity or (here: empty) capability set.
SIGNAL_COMPOSER_EDITABLE_FIELDS: tuple[str, ...] = (
    "model_route_name",
    "context_compaction",
    "max_turns",
)


def is_signal_composer_agent(agent: str | Mapping[str, Any] | None) -> bool:
    """True for the fixed signal-card composer agent, by id.

    Unlike :func:`doyoutrade.assistant.main_agent.is_main_agent` this does NOT
    key on the ``is_builtin`` flag (which the main agent also sets), because
    both builtins carry it and they must stay distinguishable. Id match only.
    """
    if agent is None:
        return False
    if isinstance(agent, str):
        return agent == SIGNAL_COMPOSER_AGENT_ID
    return agent.get("id") == SIGNAL_COMPOSER_AGENT_ID
