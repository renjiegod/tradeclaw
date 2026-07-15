# doyoutrade/persistence/strategy_storage.py
"""On-disk versioned storage for strategy code.

Layout::

    <root>/<definition_id>/
        versions/
            v0001-<hash16>/strategy.py
            v0002-<hash16>/strategy.py
        drafts/
            <session_id>/strategy.py

The storage layer is the *only* place that knows about the filesystem
layout. All callers (repository, compiler, file tools, authoring tools)
go through this module so the layout can evolve without rippling.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional


SCAFFOLD_STRATEGY_PY = '''"""Author-supplied strategy.

The entry file must be named ``strategy.py`` and define a class named
``Strategy`` that subclasses ``doyoutrade.strategy_sdk.Strategy``.

Helper modules can live next to this file (e.g. ``helpers.py``,
``indicators/ma.py``); import them with normal absolute imports
("from helpers import ...") — the directory is added to ``sys.path``
at load time.
"""
from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal


class Strategy(BaseStrategy):
    startup_history = 30

    def on_bar(self, df, ctx):
        return Signal.hold()
'''


class StrategyStorageError(Exception):
    pass


class SandboxViolation(StrategyStorageError):
    pass


class DraftNotFound(StrategyStorageError):
    pass


class VersionNotFound(StrategyStorageError):
    pass


class EmptyDraft(StrategyStorageError):
    pass


# 16 hex chars = 64 bits of SHA-256: collision-unlikely for per-definition
# version dirs and short enough for human-readable version labels.
_HASH_PREFIX_LEN = 16


# Generated / junk artifacts that may appear inside a draft or version dir but
# are NOT author source. CPython writes ``__pycache__/<mod>.pyc`` next to a
# module whenever the strategy is imported (compile / smoke / load), so version
# dirs accumulate binary bytecode. Enumerating it as a "file" leaks binary into
# the API (decoded as UTF-8 → 乱码 in the source viewer) and lets it perturb the
# content hash. The storage layer owns the on-disk layout, so it is the single
# place to filter these out for every consumer (file tools, detail API, hash).
_IGNORED_DIR_NAMES = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})


def _is_generated_artifact(rel: Path) -> bool:
    if any(part in _IGNORED_DIR_NAMES for part in rel.parts):
        return True
    if rel.suffix in _IGNORED_SUFFIXES:
        return True
    if rel.name in _IGNORED_FILE_NAMES:
        return True
    return False


class StrategyStorage:
    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    # --- path helpers ---

    @property
    def root(self) -> Path:
        return self._root

    def definition_dir(self, definition_id: str) -> Path:
        self._assert_safe_id(definition_id)
        return self._root / definition_id

    def versions_dir(self, definition_id: str) -> Path:
        return self.definition_dir(definition_id) / "versions"

    def drafts_dir(self, definition_id: str) -> Path:
        return self.definition_dir(definition_id) / "drafts"

    def version_dir(self, definition_id: str, version_label: str) -> Path:
        self._assert_safe_id(version_label)
        path = self.versions_dir(definition_id) / version_label
        if not path.exists():
            raise VersionNotFound(f"version {version_label} not found for {definition_id}")
        return path

    def draft_dir(self, definition_id: str, session_id: str) -> Path:
        self._assert_safe_id(session_id)
        return self.drafts_dir(definition_id) / session_id

    # --- lifecycle ---

    def open_draft(
        self,
        definition_id: str,
        session_id: str,
        *,
        base_version: Optional[str],
    ) -> Path:
        draft = self.draft_dir(definition_id, session_id)
        if draft.exists():
            raise StrategyStorageError(
                f"draft already exists: {definition_id}/{session_id}"
            )
        draft.parent.mkdir(parents=True, exist_ok=True)
        if base_version is None:
            draft.mkdir()
            (draft / "strategy.py").write_text(SCAFFOLD_STRATEGY_PY)
        else:
            source = self.version_dir(definition_id, base_version)
            shutil.copytree(source, draft)
        return draft

    def cancel_draft(self, definition_id: str, session_id: str) -> None:
        draft = self.draft_dir(definition_id, session_id)
        if not draft.exists():
            raise DraftNotFound(f"no draft to cancel: {session_id}")
        shutil.rmtree(draft)

    def finalize_draft(self, definition_id: str, session_id: str) -> tuple[str, str]:
        draft = self.draft_dir(definition_id, session_id)
        if not draft.exists():
            raise DraftNotFound(f"no draft to finalize: {session_id}")
        if not any(draft.rglob("*.py")):
            raise EmptyDraft(f"draft {session_id} contains no .py files")
        code_hash = self.compute_hash(draft)
        versions = self.versions_dir(definition_id)
        versions.mkdir(parents=True, exist_ok=True)
        next_n = self._next_version_number(versions)
        version_label = f"v{next_n:04d}-{code_hash}"
        target = versions / version_label
        # Atomic enough: rename within same filesystem
        os.rename(draft, target)
        return version_label, code_hash

    # --- file ops within sandbox ---

    def resolve_in_sandbox(self, work_dir: Path, relative_or_abs: str) -> Path:
        """Resolve ``relative_or_abs`` against ``work_dir`` and reject any
        path that escapes via ``..`` or symlinks. Used by file tools."""
        work_dir = work_dir.resolve(strict=True)
        candidate = Path(relative_or_abs)
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SandboxViolation(f"cannot resolve {relative_or_abs}: {exc}")
        try:
            resolved.relative_to(work_dir)
        except ValueError:
            raise SandboxViolation(
                f"path escapes sandbox: {relative_or_abs} -> {resolved}"
            )
        # Stage 2: catch symlinks whose target's true on-disk path escapes
        # the sandbox even though ``resolve(strict=False)`` already followed
        # them. ``strict=True`` raises if the target is missing, which we
        # convert to a SandboxViolation rather than leaking FileNotFoundError.
        if candidate.exists() or candidate.is_symlink():
            try:
                true_target = candidate.resolve(strict=True)
            except OSError as exc:
                raise SandboxViolation(
                    f"could not resolve sandbox path {relative_or_abs}: {exc}"
                )
            try:
                true_target.relative_to(work_dir)
            except ValueError:
                raise SandboxViolation(
                    f"symlink target escapes sandbox: {relative_or_abs}"
                )
        return resolved

    def list_files(self, work_dir: Path) -> list[str]:
        work_dir = work_dir.resolve(strict=True)
        out: list[str] = []
        for p in sorted(work_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(work_dir)
            if _is_generated_artifact(rel):
                continue
            out.append(str(rel))
        return out

    # --- hashing ---

    def compute_hash(self, work_dir: Path) -> str:
        work_dir = work_dir.resolve(strict=True)
        h = hashlib.sha256()
        for path in sorted(work_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(work_dir)
            if _is_generated_artifact(rel_path):
                continue
            rel = rel_path.as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
        return h.hexdigest()[:_HASH_PREFIX_LEN]

    # --- internals ---

    def _next_version_number(self, versions_dir: Path) -> int:
        if not versions_dir.exists():
            return 1
        ns: list[int] = []
        for child in versions_dir.iterdir():
            name = child.name
            if not name.startswith("v"):
                continue
            try:
                ns.append(int(name[1:].split("-", 1)[0]))
            except ValueError:
                continue
        return (max(ns) + 1) if ns else 1

    @staticmethod
    def _assert_safe_id(value: str) -> None:
        if not value or "/" in value or ".." in value or value.startswith("."):
            raise SandboxViolation(f"unsafe path component: {value!r}")
