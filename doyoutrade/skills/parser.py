"""Parse SKILL.md with YAML frontmatter."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from doyoutrade.skills.types import Skill

logger = logging.getLogger(__name__)


def parse_skill_file(
    skill_file: Path,
    *,
    relative_path: Path | None = None,
) -> Skill | None:
    """Parse ``SKILL.md`` into :class:`Skill`, or ``None`` if invalid."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Cannot read skill file %s: %s", skill_file, e)
        return None

    if not text.startswith("---"):
        logger.warning("Skill %s missing YAML frontmatter", skill_file)
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("Skill %s has broken frontmatter", skill_file)
        return None

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        logger.warning("Skill %s invalid YAML: %s", skill_file, e)
        return None

    if not isinstance(meta, dict):
        return None

    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        logger.warning("Skill %s missing name", skill_file)
        return None
    if not isinstance(description, str) or not description.strip():
        logger.warning("Skill %s missing description", skill_file)
        return None

    skill_type = meta.get("type", "standard")
    if skill_type not in ("standard", "flow"):
        # A typo'd type must not silently load as a plain skill (the flow
        # engine would never engage); reject loudly like other frontmatter
        # violations so the author sees it in skill listings going missing.
        logger.warning(
            "Skill %s has unsupported type %r (expected 'standard' or 'flow')",
            skill_file,
            skill_type,
        )
        return None

    body = parts[2].lstrip("\n")
    rel = relative_path if relative_path is not None else Path(".")
    lic = meta.get("license")
    license_str = str(lic) if lic is not None else None

    return Skill(
        name=name.strip(),
        description=description.strip(),
        skill_dir=skill_file.parent,
        skill_file=skill_file,
        relative_path=rel,
        body=body,
        license=license_str,
        enabled=True,
        skill_type=skill_type,
    )
