"""Slash command resolution and skill invocation message building."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doyoutrade.skills.types import Skill

# Slash command pattern: starts with "/" followed by skill name
_SLASH_PATTERN = re.compile(r"^/(\S+?)(?:\s+(.*))?$", re.DOTALL)

# Cache: canonical skill key → Skill object (lazy built on first access)
_skill_commands_cache: dict[str, Skill] | None = None


def _normalize_skill_key(name: str) -> str:
    """Lowercase, strip whitespace, replace underscores with hyphens."""
    return name.strip().lower().replace("_", "-")


def _build_cache() -> None:
    """Lazily build slash-command → Skill mapping from load_skills()."""
    global _skill_commands_cache
    if _skill_commands_cache is not None:
        return

    from doyoutrade.skills.loader import load_skills

    _skill_commands_cache = {}
    for skill in load_skills(enabled_only=True):
        key = _normalize_skill_key(skill.name)
        _skill_commands_cache[key] = skill
        # Also index by skill_path if present
        if skill.skill_path:
            path_key = _normalize_skill_key(skill.skill_path)
            if path_key not in _skill_commands_cache:
                _skill_commands_cache[path_key] = skill


def invalidate_skill_commands_cache() -> None:
    """Force the slash-command cache to be rebuilt on next access."""
    global _skill_commands_cache
    _skill_commands_cache = None


def resolve_skill_command_key(message: str) -> str | None:
    """
    Parse a slash command from a message string.

    Returns the canonical skill key (lowercased) if the message looks like a slash
    command and a matching skill exists, otherwise None.

    Examples:
        "/technical-basic"           → "technical-basic"
        "/technical-basic momentum"  → "technical-basic"
        "/TECHNICAL-BASIC"          → "technical-basic"
        "hello world"               → None
    """
    if not message or not message.startswith("/"):
        return None

    match = _SLASH_PATTERN.match(message.strip())
    if not match:
        return None

    raw_name = match.group(1)
    canonical = _normalize_skill_key(raw_name)

    # Ensure cache is built
    _build_cache()
    assert _skill_commands_cache is not None

    if canonical in _skill_commands_cache:
        return canonical
    return None


def _load_skill_payload(skill_name: str) -> Skill | None:
    """Load a skill by canonical name from the cache."""
    _build_cache()
    assert _skill_commands_cache is not None
    return _skill_commands_cache.get(skill_name)


def build_skill_invocation_message(
    cmd_key: str,
    user_instruction: str | None,
) -> str | None:
    """
    Build the user-message string that injects a skill's full content.
    Aligns with hermes-agent _build_skill_message() style.

    Returns None if the skill is not found.

    Format:
        <invoke_skill_loaded skill="technical-basic" args="momentum">
        [IMPORTANT: The user has invoked the "technical-basic" skill...]
        ## Skill Title
        ...skill body...

        [Skill directory: /path/to/skill]
        </invoke_skill_loaded>
    """
    skill = _load_skill_payload(cmd_key)
    if skill is None:
        return None

    activation_note = (
        f"[IMPORTANT: The user has invoked the \"{skill.name}\" skill, indicating they want "
        "you to follow its instructions. The full skill content is loaded below.]"
    )
    user_instr_note = (
        f"\n[User instruction: {user_instruction}]"
        if user_instruction
        else ""
    )
    dir_note = f"\n[Skill directory: {skill.skill_dir}]"

    body_lines = [activation_note, skill.body + user_instr_note, dir_note]
    return (
        f'<invoke_skill_loaded skill="{skill.name}">\n'
        + "\n".join(body_lines)
        + "\n</invoke_skill_loaded>"
    )
