"""Build a <system-reminder> HumanMessage carrying skill content loaded earlier
in this session so the model can continue using them after context compaction
folded the original load_skill tool_result into a summary boundary.

This mirrors how a coding agent re-attaches previously loaded skill content
after a context-compaction boundary folds the original tool_result into a
summary, so the model does not have to re-issue ``load_skill``.

The reminder is constructed from rows persisted by
:class:`SqlAlchemyAssistantLoadedSkillRepository`. Each row is a SKILL.md body
that was previously injected into history as a ``tool_result`` block for
``load_skill``; after compaction the model would otherwise have to re-call
``load_skill`` because the original tool_result is now folded into the
summary boundary. This module rebuilds the equivalent reminder in-memory.

Budgets (mirrors Claude Code's POST_COMPACT_SKILLS_TOKEN_BUDGET semantics):

* ``LOADED_SKILLS_TOKENS_PER_SKILL``: a per-skill cap so one oversized body
  cannot eat the whole budget.
* ``LOADED_SKILLS_TOTAL_BUDGET``: a hard ceiling across all skills; once
  exceeded we drop the oldest skills (rows are sorted newest-first first).
"""
from __future__ import annotations

import logging
from typing import Any

try:  # Match service.py's HumanMessage import strategy.
    from langchain_core.messages import HumanMessage  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - dependency fallback for stripped test envs
    from doyoutrade.test_messages import HumanMessage  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

LOADED_SKILLS_TOKENS_PER_SKILL = 5_000
LOADED_SKILLS_TOTAL_BUDGET = 25_000

# Rough byte→token estimate; aligns with the heuristic used elsewhere in the
# assistant. Conservative (4 chars/token) so we err on the side of staying
# within budget rather than overflowing it.
_BYTES_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + _BYTES_PER_TOKEN - 1) // _BYTES_PER_TOKEN)


def _truncate_to_token_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    """Truncate text to roughly ``max_tokens`` tokens.

    Returns (truncated_text, was_truncated). Cuts on a byte boundary and
    falls back to a safe UTF-8 codepoint boundary via ``errors="ignore"``;
    we are not parsing the body, so a coarse cut with a marker is fine.
    """
    target_bytes = max_tokens * _BYTES_PER_TOKEN
    raw = text.encode("utf-8")
    if len(raw) <= target_bytes:
        return text, False
    cut = raw[:target_bytes]
    decoded = cut.decode("utf-8", errors="ignore")
    return decoded + "\n\n[...truncated to fit per-skill budget]", True


async def build_loaded_skills_reminder(
    session_id: str,
    repository: Any,  # SqlAlchemyAssistantLoadedSkillRepository
) -> HumanMessage | None:
    """Pull invoked skills for this session, format as a <system-reminder>.

    Returns None when:

    * the session has no loaded skills,
    * the repository read raised (logged + degraded, not re-raised — the
      model can still answer, it just lacks the loaded-skill reminder).

    Raises ``ValueError`` if ``session_id`` is empty: empty session ids are
    a programming bug at the call site, not a repository condition; per
    CLAUDE.md §错误可见性 we surface the schema violation rather than
    silently coercing it.
    """
    if not session_id:
        raise ValueError(
            f"session_id must be non-empty, got {session_id!r}"
        )

    try:
        rows = await repository.list_by_session(session_id)
    except Exception as exc:
        logger.warning(
            "loaded_skills_reminder.unavailable session_id=%s error_type=%s message=%s",
            session_id, type(exc).__name__, exc,
        )
        return None

    if not rows:
        return None

    # Sort newest first so eviction under the total budget drops the oldest
    # skills, matching Claude Code's POST_COMPACT_SKILLS_TOKEN_BUDGET behavior.
    # ``list_by_session`` already orders by ``loaded_at`` desc, but we resort
    # defensively in case the row order changes.
    rows = sorted(
        rows,
        key=lambda r: r.get("loaded_at") or "",
        reverse=True,
    )

    parts: list[str] = [
        "<system-reminder>",
        "The following skills were loaded earlier in this session and remain in effect. "
        "You do not need to call `load_skill` again for them.",
        "",
    ]
    used_tokens = 0
    included = 0
    for row in rows:
        body, _ = _truncate_to_token_budget(
            str(row.get("body") or ""), LOADED_SKILLS_TOKENS_PER_SKILL
        )
        section = (
            f"# {row.get('skill_name')}\n"
            f"Path: {row.get('skill_path')}\n"
            f"(loaded at {row.get('loaded_at')})\n\n"
            f"{body}\n"
        )
        section_tokens = _estimate_tokens(section)
        if used_tokens + section_tokens > LOADED_SKILLS_TOTAL_BUDGET:
            # Total budget exhausted; drop this and any older skills.
            break
        parts.append(section)
        used_tokens += section_tokens
        included += 1

    if included == 0:
        return None

    parts.append("</system-reminder>")
    return HumanMessage(content="\n".join(parts))
