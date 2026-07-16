"""Resolves the git tag/commit this build was made from, for display alongside
``doyoutrade.__version__`` (e.g. a version badge in the frontend, so an issue
reporter can state exactly which build they hit).

Build time: ``hatch_build.py`` freezes ``git describe`` / ``git rev-parse``
output into ``doyoutrade/_git_version.json`` and bundles it into the wheel, so
installed packages (no ``.git`` checkout present) still know their provenance.
Dev / editable installs (no such file, running from a git checkout) fall back
to invoking git directly against the repo root, cached after the first call.
"""

from __future__ import annotations

import json
import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

_GENERATED_FILE = Path(__file__).with_name("_git_version.json")


class GitVersionInfo(TypedDict):
    tag: str | None
    commit: str | None
    commit_short: str | None
    dirty: bool | None


_UNKNOWN: GitVersionInfo = {
    "tag": None,
    "commit": None,
    "commit_short": None,
    "dirty": None,
}


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.info("version_info git command failed args=%s error=%s", args, exc)
        return None
    return result.stdout.strip() or None


def _resolve_from_git_checkout() -> GitVersionInfo:
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / ".git").exists():
        return dict(_UNKNOWN)
    dirty_status = _run_git(["status", "--porcelain"], repo_root)
    return {
        "tag": _run_git(["describe", "--tags", "--always", "--dirty"], repo_root),
        "commit": _run_git(["rev-parse", "HEAD"], repo_root),
        "commit_short": _run_git(["rev-parse", "--short", "HEAD"], repo_root),
        "dirty": bool(dirty_status) if dirty_status is not None else None,
    }


@lru_cache(maxsize=1)
def get_git_version_info() -> GitVersionInfo:
    """Git provenance for the running build. Cached — resolved once per process."""
    if _GENERATED_FILE.is_file():
        try:
            data = json.loads(_GENERATED_FILE.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "version_info failed to read generated git version file path=%s error=%s",
                _GENERATED_FILE,
                exc,
            )
        else:
            return {
                "tag": data.get("tag"),
                "commit": data.get("commit"),
                "commit_short": data.get("commit_short"),
                "dirty": data.get("dirty"),
            }
    return _resolve_from_git_checkout()


__all__ = ["GitVersionInfo", "get_git_version_info"]
