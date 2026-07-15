"""Build skill metadata listing for system prompt injection.

Only the skill name + description are listed. The full SKILL.md body is
loaded on demand via the ``load_skill`` tool. This block is intentionally
metadata-only: it is a reference catalog for choosing which documentation
to load, not an execution surface.
"""

from __future__ import annotations

from doyoutrade.skills.loader import load_skills
from doyoutrade.skills.types import Skill


def get_agent_skills(skill_names: list[str]) -> list[Skill]:
    """
    Return the list of Skill objects that match the given skill_names.
    Invalid names are silently ignored.
    """
    if not skill_names:
        return []

    all_skills = load_skills(enabled_only=True)
    name_set = {str(n).strip() for n in skill_names}
    return [s for s in all_skills if s.name in name_set]


def build_preloaded_skills_prompt(skill_names: list[str]) -> str:
    """
    Build a metadata-only skill listing scoped to the agent's ``skill_names``.

    Format:
        ## Reference Skills (load with `load_skill` when you need the full guide)
        - skill-name: one-line description
        - ...

    Returns an empty string when ``skill_names`` is empty or no match is found.
    """
    skills = get_agent_skills(skill_names)
    if not skills:
        return ""

    lines = [
        "## Reference Skills (load with `load_skill` when you need the full guide)",
        "Treat this as a documentation catalog first; execute real work through the tool layer described above.",
    ]
    for skill in skills:
        description = (skill.description or "").strip().replace("\n", " ")
        lines.append(f"- {skill.name}: {description}" if description else f"- {skill.name}")
    return "\n".join(lines)
