"""Auto-smoke gate for the strategy authoring lifecycle.

Previously used by the now-deleted ``generate_strategy_definition`` and
``update_strategy_definition`` tools.  As of the strategy-as-files refactor
(Task 6, 2026-05-24), the agent entry point is ``compile_strategy_draft``
which calls :func:`run_directory_smoke_gate` directly.

Behaviour:

- ``run_smoke_gate`` first runs a *loose* compile. Loose compile failures
  (``syntax_error``, ``class_name_mismatch``, ``missing_generate``,
  ``history_check_literal_disallowed``, ...) return ``None`` — the
  caller's registry write path will re-compile and surface those with the
  full ``authoring_contract`` payload the tool catches use, preserving
  the existing UX.
- On loose-compile success the gate runs the strict-only authoring
  checks (``missing_signal_semantics`` /
  ``unsupported_signal_semantics``) and the runtime smoke
  (``runtime_smoke_failed`` / ``smoke_output_invalid`` /
  ``smoke_signal_flap_on_steady_bar``). Strict failures are wrapped into
  a :class:`StrategySmokeResult` so the caller's existing "smoke failed"
  branch surfaces them with ``persisted: false``.
- ``smoke_error_payload`` formats a failed smoke into the same JSON shape
  the validate tool uses (``error_code`` / ``stage`` / ``error_type`` /
  ``traceback_excerpt`` / ``validation_errors`` / ``repair_hints``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doyoutrade.strategy_runtime.compiler import (
    StrategyCompileResult,
    StrategyCompiler,
    StrategySmokeResult,
)


_STRICT_ONLY_ERROR_CODES: frozenset[str] = frozenset(
    {"missing_signal_semantics", "unsupported_signal_semantics"}
)


def _smoke_result_from_compile_failure(
    compile_result: StrategyCompileResult,
) -> StrategySmokeResult:
    """Wrap a strict-only authoring-time compile failure into ``StrategySmokeResult``.

    Reusing the smoke-result envelope keeps the error vocabulary
    identical between strict-compile and smoke failures, so the calling
    tool's "smoke failed" branch handles both with ``persisted: false``.
    """

    top_error = compile_result.errors[0] if compile_result.errors else (
        f"{compile_result.error_code or 'compile_error'} (no message)"
    )
    error_code = compile_result.error_code or "compile_error"
    return StrategySmokeResult(
        success=False,
        error_code=error_code,
        error_type=error_code,
        error_message=str(top_error),
        traceback_excerpt=str(top_error),
        validation_errors=tuple(
            dict(item) for item in compile_result.validation_errors
        ),
        repair_hints=tuple(compile_result.repair_hints),
    )


# TODO(task-5): collapse into run_directory_smoke_gate once string-based
# callers (resource_tools.py, authoring_tools.py) are rewired.
def run_smoke_gate(
    source_code: str,
    class_name: str,
    *,
    compiler: StrategyCompiler | None = None,
) -> StrategySmokeResult | None:
    """Run the loose compile + strict-only authoring check + smoke.

    Return value contract:

    - ``None`` — the *loose* compile failed; the caller's registry write
      path will re-compile and surface the error with its existing
      vocabulary (and ``authoring_contract``, for tools that wrap the
      registry exception).
    - ``StrategySmokeResult(success=True)`` — both gates passed.
    - ``StrategySmokeResult(success=False, ...)`` — either a strict-only
      authoring violation (``missing_signal_semantics`` /
      ``unsupported_signal_semantics``) or a runtime smoke failure.
    """

    compiler = compiler or StrategyCompiler()

    # Loose compile: detect ``class_name_mismatch`` / ``syntax_error`` etc.
    # without raising the strict-only checks. These flow through the
    # registry's compile path so the existing exception → authoring
    # contract surfacing remains intact.
    loose = compiler.validate_definition(source_code, class_name)
    if not loose.success or loose.artifact is None:
        return None

    # Strict-only authoring violations (e.g. ``signal_semantics`` was not
    # declared in the source body). The base class default keeps these
    # OK at runtime, so they only matter at authoring time.
    strict = compiler.validate_definition(
        source_code, class_name, strict_authoring=True
    )
    if not strict.success and (strict.error_code or "") in _STRICT_ONLY_ERROR_CODES:
        return _smoke_result_from_compile_failure(strict)

    return compiler.smoke_test(loose.artifact)


def smoke_error_payload(
    smoke: StrategySmokeResult,
    *,
    class_name: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format a failed smoke into the validate-tool's error JSON shape.

    ``stage`` is ``compile`` when the failure is a strict-only authoring
    violation (so the caller can disambiguate vocabulary if needed) and
    ``smoke`` otherwise.
    """

    stage = "compile" if (smoke.error_code or "") in _STRICT_ONLY_ERROR_CODES else "smoke"
    payload: dict[str, Any] = {
        "status": "error",
        "error_code": smoke.error_code or "runtime_smoke_failed",
        "stage": stage,
        "class_name": class_name,
        "error_type": smoke.error_type,
        "error_message": smoke.error_message,
        "traceback_excerpt": smoke.traceback_excerpt,
        "validation_errors": [dict(item) for item in smoke.validation_errors],
        "repair_hints": list(smoke.repair_hints),
    }
    if extra:
        payload.update(extra)
    return payload


def smoke_error_header(smoke: StrategySmokeResult) -> tuple[str, str | None]:
    """Return (error_code, top_repair_hint) for the human header line."""

    error_code = smoke.error_code or "runtime_smoke_failed"
    hint = smoke.repair_hints[0] if smoke.repair_hints else None
    return error_code, hint


def run_directory_smoke_gate(
    code_root: Path,
    *,
    compiler: StrategyCompiler | None = None,
) -> StrategySmokeResult:
    """Run compilation + runtime smoke for a strategy directory tree.

    Unlike :func:`run_smoke_gate`, this function compiles from a
    ``code_root`` directory (via :meth:`StrategyCompiler.validate_directory`)
    and always returns a :class:`StrategySmokeResult` — never ``None``.
    A compile failure produces ``success=False`` with the compile error
    wrapped in the smoke envelope.

    The four synthetic-data regimes (monotone / flat / zigzag / step_up)
    are run via :meth:`StrategyCompiler.smoke_test` exactly as in the
    string-based gate.
    """
    compiler = compiler or StrategyCompiler()
    compile_result = compiler.validate_directory(code_root)
    if not compile_result.ok or compile_result.artifact is None:
        return _smoke_result_from_compile_failure(compile_result)
    return compiler.smoke_test(compile_result.artifact)


__all__ = [
    "run_directory_smoke_gate",
    "run_smoke_gate",
    "smoke_error_header",
    "smoke_error_payload",
]
