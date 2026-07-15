"""Skill metadata (deer-flow–style SKILL.md, flat root)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    """Parsed skill: frontmatter + markdown body."""

    name: str
    description: str
    skill_dir: Path
    skill_file: Path
    relative_path: Path
    body: str
    license: str | None = None
    enabled: bool = True
    # "standard" (plain instructions) or "flow" (body embeds one mermaid
    # flowchart that the assistant runtime walks node by node — see
    # doyoutrade/skills/flow.py).
    skill_type: str = "standard"

    @property
    def skill_path(self) -> str:
        rel = self.relative_path.as_posix()
        return "" if rel == "." else rel

    def display_location(self, root_label: str) -> str:
        """Human-readable path hint for prompts."""
        if self.skill_path:
            return f"{root_label}/{self.skill_path}/SKILL.md"
        return f"{root_label}/SKILL.md"
