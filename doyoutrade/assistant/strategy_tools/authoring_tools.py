"""Authoring session lifecycle tools for strategy-as-files.

Four tools form a state machine around ``StrategyStorage.drafts/<session_id>/``:

- ``open_strategy_authoring``   — create or copy a draft
- ``cancel_strategy_authoring`` — discard the draft
- ``compile_strategy_draft``    — AST + smoke validation; no DB write
- ``finalize_strategy_authoring``— validate, promote to versioned dir, update DB

Session state lives on the filesystem only (presence of
``<definition_id>/drafts/<session_id>/``). No separate DB table.

Design choices (sync vs async):
  These tools are ``OperationHandler`` subclasses whose ``execute()`` is
  ``async`` — matching every other tool in the registry. Repository calls
  are also ``async`` (as documented in repositories.py). Both halves are
  naturally async, so no ``asyncio.run()`` wrapper is needed.

``set_current_version`` decision:
  We call ``repository.update_definition(definition_id, current_version=label)``
  directly rather than adding a new repo method. The intent is clear from
  the keyword argument, and adding a one-liner method would only be noise.

Error codes (stable tokens, referenced in skill docs):
  - ``name_required_for_new_definition``
  - ``definition_not_found``
  - ``session_not_found``
  - ``empty_draft``
  - ``strategy_no_current_version``   (also used by InstanceSignalGenerator)
  - Compiler error_codes propagated verbatim from StrategyCompiler.validate_directory
"""
from __future__ import annotations

import secrets
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from doyoutrade.debug import emit_debug_event
from doyoutrade.persistence.strategy_storage import (
    DraftNotFound,
    EmptyDraft,
    StrategyStorage,
)
from doyoutrade.strategy_runtime.compiler import StrategyCompiler
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text
from doyoutrade.tools import _sandbox as _file_sandbox

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared session-location helper
# ---------------------------------------------------------------------------


class SessionNotFound(Exception):
    """Raised when locate_session cannot find a draft dir for the session_id."""


def locate_session(storage: StrategyStorage, session_id: str) -> tuple[str, Path]:
    """Scan the storage root for a draft directory named ``session_id``.

    Returns ``(definition_id, work_dir)`` when found.
    Raises :class:`SessionNotFound` when no draft matches.

    The draft layout is::

        <root>/<definition_id>/drafts/<session_id>/

    We iterate definition-level subdirectories rather than scanning
    everything so we can return the ``definition_id`` alongside the path.

    Performance note: this performs an O(N) scan over all definition
    directories under ``storage.root``. For a single user with O(10–100)
    strategy definitions the scan cost is negligible. Callers that need to
    look up many sessions should cache the result rather than repeatedly
    calling this function.
    """
    root = storage.root
    for defn_dir in root.iterdir():
        if not defn_dir.is_dir():
            continue
        drafts_dir = defn_dir / "drafts"
        if not drafts_dir.is_dir():
            continue
        candidate = drafts_dir / session_id
        if candidate.is_dir():
            return defn_dir.name, candidate
    raise SessionNotFound(f"no draft found for session_id={session_id!r}")


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


@dataclass
class _AuthoringToolBase(OperationHandler):
    """Holds the two shared dependencies for the four lifecycle tools.

    ``repository`` uses ``async def`` (SqlAlchemyStrategyDefinitionRepository
    pattern). ``compiler`` is stateless.
    """

    storage: StrategyStorage
    repository: Any  # SqlAlchemyStrategyDefinitionRepository
    compiler: StrategyCompiler

    # OperationHandler expects a parameters dict; base gives an empty one.
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    def _locate_session(self, session_id: str) -> tuple[str, Path]:
        """Thin wrapper so subclasses don't import ``locate_session`` directly."""
        return locate_session(self.storage, session_id)


# ---------------------------------------------------------------------------
# 1. open_strategy_authoring
# ---------------------------------------------------------------------------


@dataclass
class OpenStrategyAuthoringTool(_AuthoringToolBase):
    """Open a new authoring session for an existing or brand-new definition.

    When ``definition_id`` is supplied the current version is copied into
    a new draft. When only ``name`` is supplied a new definition is created
    and the scaffold template is written into the draft.

    Returns::

        {
          "status": "ok" | "created",
          "definition_id": "sd-...",
          "session_id":    "sess-...",
          "work_dir":      "/absolute/path/to/draft",
          "base_version":  "v0001-abc..." | null,
        }

    Error codes: ``name_required_for_new_definition``, ``definition_not_found``.
    """

    name: str = "open_strategy_authoring"
    description: str = (
        "Open a strategy authoring session. When definition_id is supplied, "
        "copies the current version into a new draft. When only name is supplied, "
        "creates a new definition with the scaffold template. Returns "
        "session_id and work_dir for subsequent file/compile/finalize calls."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "definition_id": {
                "type": "string",
                "description": "Existing strategy definition id (sd-...). Omit to create a new definition.",
            },
            "name": {
                "type": "string",
                "description": "Human-readable name for a NEW definition. Required when definition_id is omitted.",
            },
        },
        "required": [],
    }

    async def execute(
        self,
        definition_id: str | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "definition_id": definition_id,
            "name": name,
        }
        await emit_debug_event(f"operation_{self.name}.request", base_payload)

        contract = self._enforce_kwargs_contract(
            {k: v for k, v in {"definition_id": definition_id, "name": name, **kwargs}.items()
             if v is not None}
        )
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": contract.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(contract.error.get("type", "validation_error")),
                    str(contract.error.get("message", "invalid arguments")),
                ),
                is_error=True,
            )

        session_id = "sess-" + secrets.token_hex(6)

        # --- branch: open existing definition ---
        if definition_id is not None:
            try:
                defn = await self.repository.get_definition(definition_id)
            except Exception as exc:
                logger.warning(
                    "open_strategy_authoring definition_not_found definition_id=%s exc=%s",
                    definition_id, exc,
                )
                await emit_debug_event(
                    f"operation_{self.name}.rejected",
                    {
                        **base_payload,
                        "error_code": "definition_not_found",
                        "hint": "verify the definition_id with list_strategy_definitions",
                    },
                )
                return ToolResult(
                    text=format_error_text(
                        "definition_not_found",
                        f"strategy definition not found: {definition_id}",
                    ),
                    is_error=True,
                )

            base_version = defn.current_version
            try:
                work_dir = self.storage.open_draft(
                    definition_id, session_id, base_version=base_version
                )
            except Exception as exc:
                logger.warning(
                    "open_strategy_authoring storage_error definition_id=%s session_id=%s exc=%s",
                    definition_id, session_id, exc,
                )
                await emit_debug_event(
                    f"operation_{self.name}.failed",
                    {
                        **base_payload,
                        "error_code": "storage_error",
                        "hint": str(exc),
                    },
                )
                return ToolResult(
                    text=format_error_text("storage_error", str(exc)),
                    is_error=True,
                )

            _file_sandbox.register_sandbox(work_dir)
            payload = {
                "status": "ok",
                "definition_id": definition_id,
                "session_id": session_id,
                "work_dir": str(work_dir),
                "base_version": base_version,
            }
            await emit_debug_event(
                f"operation_{self.name}.created",
                {
                    "definition_id": definition_id,
                    "session_id": session_id,
                    "version_label_base": base_version,
                },
            )
            header = (
                f"Opened authoring session {session_id} for definition "
                f"{definition_id} (base={base_version or 'scaffold'})."
            )
            return ToolResult(text=append_json_payload(header, payload))

        # --- branch: create new definition ---
        if not name or not name.strip():
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "error_code": "name_required_for_new_definition",
                    "hint": "supply a name when creating a new strategy definition",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "name_required_for_new_definition",
                    "name is required when definition_id is not supplied",
                ),
                is_error=True,
            )

        new_definition_id = "sd-" + uuid4().hex[:12]
        try:
            await self.repository.create_definition(
                definition_id=new_definition_id,
                name=name.strip(),
                current_version=None,
                api_version="v1",
                input_contract_json=None,
                parameter_schema_json=None,
                default_parameters_json=None,
                capabilities_json=None,
                provenance_json={"origin": "authoring_session"},
                code_hash="",
                generation_prompt="",
                generation_model="",
                generation_metadata_json=None,
                status="draft",
            )
        except Exception as exc:
            logger.warning(
                "open_strategy_authoring create_definition_failed name=%r exc=%s",
                name, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "create_definition_failed",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("create_definition_failed", str(exc)),
                is_error=True,
            )

        try:
            work_dir = self.storage.open_draft(
                new_definition_id, session_id, base_version=None
            )
        except Exception as exc:
            logger.warning(
                "open_strategy_authoring open_draft_failed definition_id=%s session_id=%s exc=%s",
                new_definition_id, session_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": new_definition_id,
                    "error_code": "storage_error",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("storage_error", str(exc)),
                is_error=True,
            )

        _file_sandbox.register_sandbox(work_dir)
        payload = {
            "status": "created",
            "definition_id": new_definition_id,
            "session_id": session_id,
            "work_dir": str(work_dir),
            "base_version": None,
        }
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                "definition_id": new_definition_id,
                "session_id": session_id,
                "version_label_base": None,
            },
        )
        header = (
            f"Created new strategy definition {new_definition_id} "
            f"({name.strip()!r}) and opened authoring session {session_id}."
        )
        return ToolResult(text=append_json_payload(header, payload))


# ---------------------------------------------------------------------------
# 2. cancel_strategy_authoring
# ---------------------------------------------------------------------------


@dataclass
class CancelStrategyAuthoringTool(_AuthoringToolBase):
    """Discard the draft for an authoring session.

    The draft directory is removed; the definition record is untouched.
    Error codes: ``session_not_found``.
    """

    name: str = "cancel_strategy_authoring"
    description: str = (
        "Discard the draft for an authoring session. "
        "The strategy definition record is untouched."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session id returned by open_strategy_authoring.",
            },
        },
        "required": ["session_id"],
    }

    async def execute(self, session_id: str, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {"tool": self.name, "session_id": session_id}
        await emit_debug_event(f"operation_{self.name}.request", base_payload)

        contract = self._enforce_kwargs_contract({"session_id": session_id, **kwargs})
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": contract.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(contract.error.get("type", "validation_error")),
                    str(contract.error.get("message", "invalid arguments")),
                ),
                is_error=True,
            )

        try:
            definition_id, _ = self._locate_session(session_id)
        except SessionNotFound:
            logger.info(
                "cancel_strategy_authoring session_not_found session_id=%s", session_id
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "error_code": "session_not_found",
                    "hint": "the session may have already been finalized or cancelled",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "session_not_found",
                    f"no active authoring session found for session_id={session_id!r}",
                ),
                is_error=True,
            )

        try:
            # Locate work_dir before cancelling so we can unregister the sandbox.
            _work_dir_for_cancel = self.storage.draft_dir(definition_id, session_id)
            self.storage.cancel_draft(definition_id, session_id)
            _file_sandbox.unregister_sandbox(_work_dir_for_cancel)
        except DraftNotFound:
            logger.info(
                "cancel_strategy_authoring draft_already_gone session_id=%s definition_id=%s",
                session_id, definition_id,
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "session_not_found",
                    "hint": "draft already removed; no action needed",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "session_not_found",
                    f"draft already removed for session_id={session_id!r}",
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.warning(
                "cancel_strategy_authoring storage_error session_id=%s definition_id=%s exc=%s",
                session_id, definition_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "storage_error",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("storage_error", str(exc)),
                is_error=True,
            )

        payload = {"status": "ok", "session_id": session_id, "definition_id": definition_id}
        await emit_debug_event(
            f"operation_{self.name}.validated",
            {"session_id": session_id, "definition_id": definition_id},
        )
        header = f"Cancelled authoring session {session_id}; draft removed."
        return ToolResult(text=append_json_payload(header, payload))


# ---------------------------------------------------------------------------
# 3. compile_strategy_draft
# ---------------------------------------------------------------------------


def _compile_error_result(result: Any) -> ToolResult:
    """Build a structured ToolResult for a compilation failure."""
    payload = {
        "status": "error",
        "error_code": result.error_code or "compile_failed",
        "errors": list(result.errors),
        "validation_errors": list(result.error_dicts),
        "repair_hints": list(result.repair_hints),
    }
    header = f"Strategy draft failed validation: {result.error_code}"
    return ToolResult(text=append_json_payload(header, payload), is_error=True)


@dataclass
class CompileStrategyDraftTool(_AuthoringToolBase):
    """Run AST + smoke validation on the draft without persisting.

    On success returns ``status: ok``. On failure returns the full compiler
    error envelope (``error_code``, ``errors``, ``validation_errors``,
    ``repair_hints``) so the model can repair the code in one pass.

    The draft is preserved regardless of outcome; finalize is still required.
    Error codes: ``session_not_found``, ``empty_draft``, compiler error codes.
    """

    name: str = "compile_strategy_draft"
    description: str = (
        "Run AST + smoke validation on the current draft without persisting. "
        "Returns status:ok on success or a full compiler error envelope on failure. "
        "Draft is preserved in either case."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session id returned by open_strategy_authoring.",
            },
        },
        "required": ["session_id"],
    }

    async def execute(self, session_id: str, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {"tool": self.name, "session_id": session_id}
        await emit_debug_event(f"operation_{self.name}.request", base_payload)

        contract = self._enforce_kwargs_contract({"session_id": session_id, **kwargs})
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": contract.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(contract.error.get("type", "validation_error")),
                    str(contract.error.get("message", "invalid arguments")),
                ),
                is_error=True,
            )

        try:
            definition_id, work_dir = self._locate_session(session_id)
        except SessionNotFound:
            logger.info(
                "compile_strategy_draft session_not_found session_id=%s", session_id
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "error_code": "session_not_found",
                    "hint": "the session may have already been finalized or cancelled",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "session_not_found",
                    f"no active authoring session found for session_id={session_id!r}",
                ),
                is_error=True,
            )

        if not any(work_dir.rglob("*.py")):
            logger.info(
                "compile_strategy_draft empty_draft session_id=%s definition_id=%s",
                session_id, definition_id,
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "empty_draft",
                    "hint": "write at least one .py file to the work_dir before compiling",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "empty_draft",
                    f"draft for session {session_id} contains no .py files",
                ),
                is_error=True,
            )

        try:
            result = self.compiler.validate_directory(work_dir)
        except Exception as exc:
            logger.warning(
                "compile_strategy_draft compiler_error session_id=%s definition_id=%s exc=%s",
                session_id, definition_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "compiler_error",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("compiler_error", str(exc)),
                is_error=True,
            )

        if not result.success:
            logger.info(
                "compile_strategy_draft validation_failed session_id=%s definition_id=%s error_code=%s",
                session_id, definition_id, result.error_code,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": result.error_code or "compile_failed",
                    "errors": list(result.errors),
                    "hint": "fix the reported compile errors and retry",
                },
            )
            return _compile_error_result(result)

        payload = {
            "status": "ok",
            "session_id": session_id,
            "definition_id": definition_id,
            "class_name": result.artifact.class_name if result.artifact else "Strategy",
        }
        await emit_debug_event(
            f"operation_{self.name}.validated",
            {
                "session_id": session_id,
                "definition_id": definition_id,
                "class_name": payload["class_name"],
            },
        )
        header = f"Strategy draft compiled successfully for session {session_id}."
        return ToolResult(text=append_json_payload(header, payload))


# ---------------------------------------------------------------------------
# 4. finalize_strategy_authoring
# ---------------------------------------------------------------------------


@dataclass
class FinalizeStrategyAuthoringTool(_AuthoringToolBase):
    """Validate, promote, and register a finalized strategy version.

    Sequence:
    1. ``locate_session`` — find the draft directory.
    2. ``compiler.validate_directory`` — must pass; on failure preserve draft.
    3. ``storage.finalize_draft`` — atomic rename to ``versions/v{N+1}-{hash}/``.
    4. ``repository.update_definition(definition_id, current_version=label)``
       — update the DB pointer.

    On success returns ``status: ok`` with ``version_label`` and ``definition_id``.
    On compile failure the draft is preserved and the error envelope is returned.
    Error codes: ``session_not_found``, ``empty_draft``, ``finalize_failed``,
    compiler error codes.
    """

    name: str = "finalize_strategy_authoring"
    description: str = (
        "Validate the draft, promote it to a versioned directory, and update the "
        "strategy definition's current_version. On compile failure the draft is "
        "preserved. Returns status:ok with version_label on success."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Session id returned by open_strategy_authoring.",
            },
        },
        "required": ["session_id"],
    }

    async def execute(self, session_id: str, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {"tool": self.name, "session_id": session_id}
        await emit_debug_event(f"operation_{self.name}.request", base_payload)

        contract = self._enforce_kwargs_contract({"session_id": session_id, **kwargs})
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": contract.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(contract.error.get("type", "validation_error")),
                    str(contract.error.get("message", "invalid arguments")),
                ),
                is_error=True,
            )

        try:
            definition_id, work_dir = self._locate_session(session_id)
        except SessionNotFound:
            logger.info(
                "finalize_strategy_authoring session_not_found session_id=%s", session_id
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "error_code": "session_not_found",
                    "hint": "the session may have already been finalized or cancelled",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "session_not_found",
                    f"no active authoring session found for session_id={session_id!r}",
                ),
                is_error=True,
            )

        if not any(work_dir.rglob("*.py")):
            logger.info(
                "finalize_strategy_authoring empty_draft session_id=%s definition_id=%s",
                session_id, definition_id,
            )
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "empty_draft",
                    "hint": "write at least one .py file to the work_dir before finalizing",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "empty_draft",
                    f"draft for session {session_id} contains no .py files",
                ),
                is_error=True,
            )

        # Step 1: compile — preserve draft on failure
        try:
            result = self.compiler.validate_directory(work_dir)
        except Exception as exc:
            logger.warning(
                "finalize_strategy_authoring compiler_error session_id=%s definition_id=%s exc=%s",
                session_id, definition_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "compiler_error",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("compiler_error", str(exc)),
                is_error=True,
            )

        if not result.success:
            logger.info(
                "finalize_strategy_authoring validation_failed session_id=%s definition_id=%s error_code=%s",
                session_id, definition_id, result.error_code,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": result.error_code or "compile_failed",
                    "errors": list(result.errors),
                    "hint": "fix the reported compile errors and retry",
                },
            )
            return _compile_error_result(result)

        # Step 2: promote to versioned directory
        # Capture the draft work_dir now so we can unregister the sandbox
        # even if finalize_draft succeeds (it renames the directory away).
        _work_dir_for_finalize = self.storage.draft_dir(definition_id, session_id)
        try:
            version_label, code_hash = self.storage.finalize_draft(definition_id, session_id)
        except EmptyDraft as exc:
            logger.warning(
                "finalize_strategy_authoring empty_draft session_id=%s definition_id=%s exc=%s",
                session_id, definition_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "empty_draft",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("empty_draft", str(exc)),
                is_error=True,
            )
        except DraftNotFound:
            logger.warning(
                "finalize_strategy_authoring session_disappeared "
                "session_id=%s definition_id=%s",
                session_id, definition_id,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "session_disappeared",
                    "hint": "re-open the strategy authoring session and retry",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "session_disappeared",
                    f"session {session_id} no longer exists (cancelled mid-finalize)",
                    "re-open the strategy authoring session and retry",
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.warning(
                "finalize_strategy_authoring finalize_failed session_id=%s definition_id=%s exc=%s",
                session_id, definition_id, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "error_code": "finalize_failed",
                    "hint": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("finalize_failed", str(exc)),
                is_error=True,
            )

        # Step 3: update DB pointer
        try:
            await self.repository.update_definition(
                definition_id,
                current_version=version_label,
                code_hash=code_hash,
                status="active",
            )
        except Exception as exc:
            logger.warning(
                "finalize_strategy_authoring update_definition_failed "
                "session_id=%s definition_id=%s version_label=%s exc=%s",
                session_id, definition_id, version_label, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "definition_id": definition_id,
                    "version_label": version_label,
                    "error_code": "update_definition_failed",
                    "hint": (
                        f"version promoted to disk but DB update failed; "
                        f"version_label={version_label}; retry update_definition manually"
                    ),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "update_definition_failed",
                    f"version promoted to disk but DB update failed: {exc}. "
                    f"version_label={version_label}; retry update_definition manually.",
                ),
                is_error=True,
            )

        # Draft promoted to versioned dir — unregister the sandbox root.
        # The promoted versions/v…/ directory is NOT registered as a new sandbox;
        # drafts only.
        _file_sandbox.unregister_sandbox(_work_dir_for_finalize)

        payload = {
            "status": "ok",
            "session_id": session_id,
            "definition_id": definition_id,
            "version_label": version_label,
            "code_hash": code_hash,
        }
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                "session_id": session_id,
                "definition_id": definition_id,
                "version_label": version_label,
                "code_hash": code_hash,
            },
        )
        header = (
            f"Finalized authoring session {session_id}: "
            f"definition {definition_id} promoted to {version_label}."
        )
        return ToolResult(text=append_json_payload(header, payload))


__all__ = [
    "locate_session",
    "SessionNotFound",
    "OpenStrategyAuthoringTool",
    "CancelStrategyAuthoringTool",
    "CompileStrategyDraftTool",
    "FinalizeStrategyAuthoringTool",
]
