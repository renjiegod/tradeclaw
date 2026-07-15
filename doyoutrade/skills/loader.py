"""Discover skills under a flat skills root (each subdir holds SKILL.md)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from doyoutrade.skills.parser import parse_skill_file
from doyoutrade.skills.types import Skill

logger = logging.getLogger(__name__)

_STATE_FILENAME = "skills_state.yaml"


def find_project_root(start: Path | None = None) -> Path:
    """
    Directory containing ``pyproject.toml``, searching upward from *start* or :func:`Path.cwd`.

    If no ``pyproject.toml`` is found, returns the starting directory (resolved).
    """
    cur = (start or Path.cwd()).resolve()
    for p in (cur, *cur.parents):
        if (p / "pyproject.toml").is_file():
            return p
    return cur


def default_skills_root() -> Path:
    """<project_root>/.doyoutrade/skills (each subdir holds SKILL.md)."""
    return find_project_root() / ".doyoutrade" / "skills"


def load_skills_state(skills_root: Path) -> dict[str, Any]:
    """Load optional ``skills_state.yaml`` (``disabled: [names]``)."""
    path = skills_root / _STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Invalid skills state %s: %s", path, e)
        return {}
    return raw if isinstance(raw, dict) else {}


def load_skills(
    skills_path: Path | None = None,
    *,
    enabled_only: bool = False,
) -> list[Skill]:
    """
    Load skills from a flat ``skills_path`` (each subdir contains ``SKILL.md``).

    If ``skills_path`` is None, uses :func:`default_skills_root`.

    When ``skills_state.yaml`` lists ``disabled: [skill_name, ...]``, those skills
    are marked ``enabled=False``. If ``enabled_only=True``, they are omitted.
    """
    root = skills_path if skills_path is not None else default_skills_root()
    if not root.is_dir():
        return []

    state = load_skills_state(root)
    disabled_raw = state.get("disabled", [])
    disabled: set[str] = set()
    if isinstance(disabled_raw, list):
        disabled = {str(x) for x in disabled_raw if isinstance(x, str)}

    skills: list[Skill] = []

    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = sorted(n for n in dir_names if not n.startswith("."))
        if "SKILL.md" not in file_names:
            continue
        skill_file = Path(current_root) / "SKILL.md"
        try:
            relative_path = skill_file.parent.relative_to(root)
        except ValueError:
            continue
        # Skip the root itself (no skill lives directly at the root)
        if relative_path == Path("."):
            continue

        skill = parse_skill_file(skill_file, relative_path=relative_path)
        if skill is None:
            continue
        if skill.name in disabled:
            skill.enabled = False
        skills.append(skill)

    skills.sort(key=lambda s: s.name)
    if enabled_only:
        skills = [s for s in skills if s.enabled]
    return skills
