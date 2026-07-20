"""Wheel build must freeze git provenance before build side-effects.

Installer / ``uv tool install`` from a clean tag still builds the frontend
inside the hatch hook. That creates gitignored *and* occasionally untracked
non-ignored paths (e.g. ``frontend/.vite``). If ``_write_git_version`` runs
after those steps, ``git status --porcelain`` falsely marks the release as
``dirty`` — which then shows up on the UI version badge for every GUI-installer
user.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

try:
    from hatch_build import CustomBuildHook
except ModuleNotFoundError as exc:  # hatchling is a build-system dep, not runtime
    CustomBuildHook = None  # type: ignore[misc, assignment]
    _HATCH_IMPORT_ERROR = exc
else:
    _HATCH_IMPORT_ERROR = None


class _FakeApp:
    def display_warning(self, message: str) -> None:
        pass

    def display_info(self, message: str) -> None:
        pass


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_clean_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="doyoutrade-git-version-test-"))
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    # Avoid global autocrlf surprises on Windows CI/dev machines.
    _git(root, "config", "core.autocrlf", "false")
    (root / "README").write_text("ok\n", encoding="utf-8")
    (root / ".gitignore").write_text("dist/\nnode_modules/\n", encoding="utf-8")
    _git(root, "add", "README", ".gitignore")
    _git(root, "commit", "-m", "init")
    _git(root, "tag", "v0.1.9")
    return root


def _make_hook(root: Path) -> CustomBuildHook:
    hook = CustomBuildHook(
        root=str(root),
        config={},
        build_config=mock.Mock(),
        metadata=mock.Mock(),
        directory=str(root / "dist"),
        target_name="wheel",
        app=_FakeApp(),
    )
    return hook


def _read_frozen_git_version(build_data: dict[str, Any]) -> dict[str, Any]:
    force_include = build_data.get("force_include") or {}
    for src, dest in force_include.items():
        if dest == "doyoutrade/_git_version.json":
            return json.loads(Path(src).read_text(encoding="utf-8"))
    raise AssertionError("doyoutrade/_git_version.json was not force-included")


@unittest.skipIf(CustomBuildHook is None, f"hatchling unavailable: {_HATCH_IMPORT_ERROR}")
class HatchGitVersionTests(unittest.TestCase):
    def test_initialize_freezes_clean_git_before_frontend_artifacts(self) -> None:
        root = _init_clean_repo()
        hook = _make_hook(root)
        build_data: dict[str, Any] = {}

        def dirty_frontend(_root: Path, _build_data: dict) -> None:
            # Simulate vite/npm side-effects that are not covered by .gitignore.
            cache = root / "frontend" / ".vite"
            cache.mkdir(parents=True)
            (cache / "deps.json").write_text("{}", encoding="utf-8")
            # Also create an ignored dist — must not affect dirty either.
            dist = root / "frontend" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<html></html>", encoding="utf-8")

        with mock.patch.object(hook, "_include_frontend", side_effect=dirty_frontend):
            with mock.patch.object(hook, "_include_qmt_proxy"):
                hook.initialize(version="0.1.9", build_data=build_data)

        info = _read_frozen_git_version(build_data)
        self.assertFalse(info["dirty"], msg=f"expected clean freeze, got {info!r}")
        self.assertEqual(info["tag"], "v0.1.9")
        self.assertTrue(info["commit"])
        self.assertTrue(info["commit_short"])

    def test_untracked_artifacts_do_not_mark_dirty_even_if_present_at_freeze(self) -> None:
        """Defense in depth: dirty ignores untracked paths, not only ordering."""
        root = _init_clean_repo()
        cache = root / "frontend" / ".vite"
        cache.mkdir(parents=True)
        (cache / "deps.json").write_text("{}", encoding="utf-8")
        hook = _make_hook(root)
        build_data: dict[str, Any] = {}

        with mock.patch.object(hook, "_include_frontend"):
            with mock.patch.object(hook, "_include_qmt_proxy"):
                hook.initialize(version="0.1.9", build_data=build_data)

        info = _read_frozen_git_version(build_data)
        self.assertFalse(info["dirty"], msg=f"expected clean freeze, got {info!r}")
        self.assertEqual(info["tag"], "v0.1.9")

    def test_tracked_modification_still_marks_dirty(self) -> None:
        root = _init_clean_repo()
        (root / "README").write_text("edited\n", encoding="utf-8")
        hook = _make_hook(root)
        build_data: dict[str, Any] = {}

        with mock.patch.object(hook, "_include_frontend"):
            with mock.patch.object(hook, "_include_qmt_proxy"):
                hook.initialize(version="0.1.9", build_data=build_data)

        info = _read_frozen_git_version(build_data)
        self.assertTrue(info["dirty"], msg=f"expected dirty freeze, got {info!r}")
        self.assertTrue(str(info["tag"]).endswith("-dirty"), msg=info)


if __name__ == "__main__":
    unittest.main()
