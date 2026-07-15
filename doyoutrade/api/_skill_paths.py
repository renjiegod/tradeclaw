"""Path-sandbox helpers for the /skills API."""

from __future__ import annotations

import mimetypes
from pathlib import Path


class SkillPathError(ValueError):
    """Raised when a requested path escapes the skills sandbox."""


_FORBIDDEN_CHARS = ("\x00",)


def resolve_skill_root(skills_root: Path, skill_id: str) -> Path:
    """Resolve ``<skills_root>/<skill_id>`` and ensure it stays inside."""
    if not skill_id or any(c in skill_id for c in _FORBIDDEN_CHARS):
        raise SkillPathError(f"invalid skill_id: {skill_id!r}")
    candidate = (skills_root / skill_id).resolve()
    root_resolved = skills_root.resolve()
    if not candidate.is_relative_to(root_resolved):
        raise SkillPathError(f"skill_id escapes skills root: {skill_id!r}")
    return candidate


def resolve_inside(skill_root: Path, relative_path: str) -> Path:
    """Resolve a path inside a skill folder, rejecting escape attempts."""
    if not relative_path or any(c in relative_path for c in _FORBIDDEN_CHARS):
        raise SkillPathError(f"invalid path: {relative_path!r}")
    if relative_path.startswith(("/", "\\")):
        raise SkillPathError(f"absolute path not allowed: {relative_path!r}")
    candidate = (skill_root / relative_path).resolve()
    root_resolved = skill_root.resolve()
    if not candidate.is_relative_to(root_resolved):
        raise SkillPathError(f"path escapes skill root: {relative_path!r}")
    return candidate


_BINARY_FALLBACK = "application/octet-stream"


def detect_mime(path: Path) -> str:
    """Best-effort MIME detection by extension; default to octet-stream."""
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    if path.suffix.lower() in {
        ".py", ".ts", ".tsx", ".js", ".jsx",
        ".yaml", ".yml", ".json", ".txt", ".toml", ".ini", ".cfg",
    }:
        return "text/plain"
    return _BINARY_FALLBACK
