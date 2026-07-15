"""In-process registry of allowed sandbox roots for the file primitives.

Lifecycle tools (e.g. ``open_strategy_authoring``) call
:func:`register_sandbox(root)` when they create a draft work_dir, and
:func:`unregister_sandbox(root)` on cancel/finalize.  The file primitives
(``read_file`` / ``write_file`` / ``edit_file`` / ``list_files``) call
:func:`resolve_path(file_path)` to verify the given absolute path lives
inside SOME registered root.

The registry is in-process (module-level set).  For pre-prod single-process
deployments this is sufficient.  If multi-process / multi-worker access
is needed in the future, this becomes a shared store keyed by
DOYOUTRADE_HOME.
"""
from __future__ import annotations

import os
from pathlib import Path

from doyoutrade.persistence.strategy_storage import SandboxViolation

_active_sandboxes: set[Path] = set()


def knowledge_root() -> Path:
    """Return the private knowledge-base root ``~/.doyoutrade/knowledge``.

    Honours ``DOYOUTRADE_HOME`` (same convention as the strategy storage root
    in ``doyoutrade/bootstrap.py``) so test / alternate-home deployments point
    at the right place. The path is expanded but not created here.
    """
    home = Path(os.getenv("DOYOUTRADE_HOME", str(Path.home() / ".doyoutrade"))).expanduser()
    return home / "knowledge"


def register_knowledge_sandbox() -> Path:
    """Register ``~/.doyoutrade/knowledge`` as a permanent writable sandbox root.

    Unlike the strategy-authoring lifecycle roots (which are registered on
    ``open`` and unregistered on ``cancel`` / ``finalize``), the knowledge
    root is never unregistered — it is a standing user-memory area the agent
    reads opportunistically and writes only when the user explicitly asks.
    Idempotent: ``register_sandbox`` stores into a set, so repeated calls
    (e.g. one per registry build) are harmless. Creates the directory so
    ``list_files`` / symlink resolution stay stable.
    """
    root = knowledge_root()
    root.mkdir(parents=True, exist_ok=True)
    register_sandbox(root)
    return root


def register_sandbox(root: Path) -> None:
    """Register *root* as an allowed sandbox directory.

    Resolves symlinks before storing so that path comparisons in
    :func:`resolve_path` use stable canonical forms.
    """
    _active_sandboxes.add(root.resolve())


def unregister_sandbox(root: Path) -> None:
    """Remove *root* from the sandbox registry.

    Silently ignores roots that were never registered (e.g. the caller
    double-cancels a session).
    """
    _active_sandboxes.discard(root.resolve())


def resolve_path(file_path: str | Path) -> Path:
    """Resolve *file_path* and verify it lives inside an active sandbox root.

    Steps:
    1. Resolve to an absolute path (``strict=False`` so non-existent targets
       are acceptable for write operations).
    2. For each registered root check ``is_relative_to`` on the resolved path.
    3. Symlink-escape check: when the target *does* exist, also resolve with
       ``strict=True`` and re-check — a symlink could point outside the root.
    4. Raise :class:`~doyoutrade.persistence.strategy_storage.SandboxViolation`
       when no registered root contains the path.

    Raises:
        SandboxViolation: path does not live inside any active sandbox.
    """
    candidate = Path(file_path).resolve(strict=False)
    for root in _active_sandboxes:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        # First check passed; also verify the strict resolution (symlink check)
        # when the file actually exists.
        if candidate.exists():
            try:
                strict_candidate = Path(file_path).resolve(strict=True)
                strict_candidate.relative_to(root)
            except (ValueError, OSError):
                raise SandboxViolation(
                    f"path {file_path!r} resolves outside registered sandbox root {root}"
                )
        return candidate
    raise SandboxViolation(
        f"path {file_path!r} is not inside any active sandbox root"
    )


__all__ = [
    "knowledge_root",
    "register_knowledge_sandbox",
    "register_sandbox",
    "unregister_sandbox",
    "resolve_path",
]
