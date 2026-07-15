"""Build skill-related prompt fragments (legacy inline system block + Claude Code–style listing)."""

from __future__ import annotations

from doyoutrade.skills.loader import load_skills


def _xml_escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_skills_listing_for_reminder(
    *,
    skills_path=None,
    available_skills: set[str] | None = None,
    max_description_chars: int = 250,
    max_total_chars: int = 8_000,
) -> str:
    """
    Compact skill catalog for a ``<system-reminder>`` block (Claude Code–style).

    One line per skill: ``- name: short description``. Full SKILL.md is loaded
    via the ``invoke_skill`` tool, not inlined here.
    """
    skills = list(load_skills(skills_path, enabled_only=True))
    if not skills:
        return ""

    if available_skills is not None:
        skills = [s for s in skills if s.name in available_skills]

    if not skills:
        return ""

    lines: list[str] = []
    for skill in skills:
        desc = (skill.description or "").strip()
        if len(desc) > max_description_chars:
            desc = desc[: max_description_chars - 1] + "…"
        lines.append(f"- {_xml_escape_text(skill.name)}: {_xml_escape_text(desc)}")

    text = "\n".join(lines)
    if len(text) > max_total_chars:
        text = text[: max_total_chars - 1] + "…"
    return text


def get_skills_prompt_section(
    *,
    skills_path=None,
    available_skills: set[str] | None = None,
    skills_root_label: str = ".doyoutrade/skills",
    max_body_chars_per_skill: int = 12_000,
) -> str:
    """
    Return an XML-ish block listing enabled skills and inlined workflow text.

    Unlike deer-flow's sandbox ``read_file`` progressive load, DoYouTrade's signal agent
    performs one model call: we inline truncated ``SKILL.md`` bodies so the model can
    follow workflows without a tool loop.
    """
    skills = list(load_skills(skills_path, enabled_only=True))
    if not skills:
        return ""

    if available_skills is not None:
        skills = [s for s in skills if s.name in available_skills]

    if not skills:
        return ""

    chunks: list[str] = []
    for skill in skills:
        loc = skill.display_location(skills_root_label)
        body = skill.body
        if len(body) > max_body_chars_per_skill:
            body = body[:max_body_chars_per_skill] + "\n\n…(skill body truncated)…"

        chunks.append(
            f"""    <skill>
        <name>{skill.name}</name>
        <description>{skill.description}</description>
        <location>{loc}</location>
        <skill_instructions>
{body}
        </skill_instructions>
    </skill>"""
        )

    skills_list = "\n".join(chunks)
    return f"""<skill_system>
You have access to skills that provide optimized workflows for specific trading-signal tasks.
Follow a skill's instructions inside <skill_instructions> when the user task matches its description.

**Skills root (reference):** {skills_root_label}

<available_skills>
{skills_list}
</available_skills>
</skill_system>"""
