"""``walk_forward_backtest`` operation — segmented out-of-sample robustness.

Borrowed (in spirit) from freqtrade's walk-forward / FreqAI ``split_timerange``
and adapted to doyoutrade's async-backtest model. The problem it solves:
every doyoutrade backtest today is a single in-sample run. An LLM that iterates
parameters against one window's report can tune a strategy into a local
optimum that only worked on that window — the reported Sharpe is then an
inflated in-sample metric with no held-out check. This is the single biggest
systematic distortion for LLM-authored strategies.

What this does: split ``[range_start, range_end]`` into N consecutive equal
windows, run the SAME strategy + parameters on each window as its own
backtest, and report per-window return / Sharpe / drawdown / trade count. If
the edge only shows up in some windows (or collapses out of sample), the
strategy is flagged ``fragile`` — its backtest result does not generalise
across time.

Honesty note (CLAUDE.md §错误可见性): this is **fixed-parameter** multi-window
OOS robustness, NOT classic walk-forward with per-window re-optimisation —
re-optimisation needs an automated parameter-search (hyperopt) primitive that
doyoutrade does not have yet. The tool says so in its output so the verdict is
not over-claimed.

Mechanics: each window is an independent backtest task (definition mode
auto-creates one), started via ``start_backtest_job`` and polled to terminal
status (the runtime advances the background job concurrently — same pattern as
the e2e ``start_backtest_and_wait`` helper). Per-window tasks are deleted after
their summary is collected unless ``keep_tasks`` is set. A window that fails to
run is reported with its ``error`` and excluded from the verdict rather than
collapsing the whole sweep.

The completed sweep returns ``is_error=False`` with a ``status`` verdict
(``robust`` / ``fragile`` / ``inconclusive``); the CLI maps ``fragile`` to a
non-zero exit so it can gate promotion, mirroring ``sdk validate-recursive``.
Hard failures (bad input, unknown definition, every window failed) return
``is_error=True`` with a stable ``error_code``.

Debug events: ``operation_walk_forward_backtest.{request, rejected, failed,
validated}`` plus per-window ``.window.{started, completed, failed}``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, timedelta
from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._identifier_kinds import IdentifierGuard, IdentifierKind
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "finished", "failed", "stopped"})
_FAILED_STATUSES = frozenset({"failed", "stopped"})
_DEFAULT_SEGMENTS = 3
_MIN_SEGMENTS = 2
_MAX_SEGMENTS = 6


class _InvalidWalkForwardArgument(ValueError):
    """Structured argument failure carrying a stable ``error_code``."""

    def __init__(
        self,
        error_code: str,
        message: str,
        hint: str | None = None,
        *,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.error_type = error_type


def _to_float(value: Any) -> float | None:
    """Parse a decimal-string / number summary cell to float, else None."""

    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _compute_windows(d0: date, d1: date, segments: int) -> list[tuple[date, date]]:
    """Split ``[d0, d1]`` into ``segments`` consecutive, non-overlapping windows.

    Boundaries are placed on evenly-spaced calendar dates; the backtest engine
    pulls its own warmup history before each window's ``range_start``, so no
    pre-window padding is needed here. Windows after the first start one day
    after the prior window's end to avoid sharing a boundary day.
    """

    total_days = (d1 - d0).days
    boundaries = [d0 + timedelta(days=round(k * total_days / segments)) for k in range(segments + 1)]
    boundaries[0] = d0
    boundaries[-1] = d1
    windows: list[tuple[date, date]] = []
    for i in range(segments):
        start = boundaries[i] if i == 0 else boundaries[i] + timedelta(days=1)
        end = boundaries[i + 1]
        if start > end:
            start = end
        windows.append((start, end))
    return windows


def _extract_window_metrics(summary_json: dict[str, Any]) -> dict[str, Any]:
    """Pull the comparison metrics out of a ``summary_to_json`` payload."""

    return {
        "return_pct": _to_float(summary_json.get("return_pct")),
        "sharpe": _to_float(summary_json.get("sharpe")),
        "max_drawdown_pct": _to_float(summary_json.get("max_drawdown_pct")),
        "win_rate": _to_float(summary_json.get("win_rate")),
        "profit_factor": _to_float(summary_json.get("profit_factor")),
        "trade_count_closed": int(summary_json.get("trade_count_closed") or 0),
        "fills_count": int(summary_json.get("fills_count") or 0),
    }


class WalkForwardBacktestTool(OperationHandler):
    name = "walk_forward_backtest"
    description = (
        "Segmented out-of-sample robustness check: split a date range into N "
        "consecutive windows and run the SAME strategy + parameters on each as "
        "its own backtest, then report per-window return / Sharpe / drawdown / "
        "trades. Flags status='fragile' when the edge does not hold across "
        "windows (the classic in-sample overfitting signal). Definition mode "
        "only: pass definition_id + universe. This is fixed-parameter "
        "multi-window OOS, not re-optimising walk-forward (which needs hyperopt)."
    )
    category = "backtest"
    parameters = {
        "type": "object",
        "properties": {
            "definition_id": {
                "type": "string",
                "description": "Strategy definition id (sd-...). Definition mode entry.",
            },
            "universe": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbols for the per-window backtests. Required.",
            },
            "parameters": {
                "type": "object",
                "description": "Parameter overrides merged with definition defaults (same across all windows).",
            },
            "range_start": {"type": "string", "description": "YYYY-MM-DD start of the full range to split."},
            "range_end": {"type": "string", "description": "YYYY-MM-DD end of the full range to split."},
            "segments": {
                "type": "integer",
                "minimum": _MIN_SEGMENTS,
                "maximum": _MAX_SEGMENTS,
                "description": f"Number of consecutive windows (default {_DEFAULT_SEGMENTS}).",
            },
            "min_trades": {
                "type": "integer",
                "minimum": 0,
                "description": "Min closed trades for a window to count toward the verdict (default 1).",
            },
            "data_provider": {"type": "string", "description": "Data source (default auto)."},
            "keep_tasks": {
                "type": "boolean",
                "description": "Keep the per-window backtest tasks for drill-in (default false → delete after).",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 1.0,
                "description": "Per-window backtest completion timeout (default 120).",
            },
        },
        "additionalProperties": False,
        "required": ["definition_id", "universe", "range_start", "range_end"],
    }

    coercion_rules = (
        SchemaCoercion(field="universe", declared_type="array", item_type=str, error_code="invalid_universe_json"),
        SchemaCoercion(field="parameters", declared_type="object", error_code="invalid_parameters_json"),
    )
    identifier_guards = (IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),)

    _DEFAULT_TIMEOUT_SECONDS = 120.0
    _POLL_INTERVAL_SECONDS = 0.25

    def __init__(self, platform_service: Any | None = None) -> None:
        self._platform_service = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_walk_forward_backtest.rejected",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            return ToolResult(
                text=format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                ),
                is_error=True,
            )
        kwargs = dict(contract.kwargs)

        guard = self._apply_identifier_guards(kwargs)
        if guard is not None:
            await emit_debug_event(
                "operation_walk_forward_backtest.rejected", {"tool": self.name, "error": guard}
            )
            from doyoutrade.tools import tool_result_from_error_dict

            return tool_result_from_error_dict(guard)

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            err = coercion.error
            await emit_debug_event("operation_walk_forward_backtest.failed", {"tool": self.name, **err})
            return ToolResult(
                text=format_error_text(
                    str(err.get("error_code") or "validation_error"),
                    str(err.get("error") or "invalid input"),
                    err.get("hint") if isinstance(err.get("hint"), str) else None,
                ),
                is_error=True,
            )
        kwargs = dict(coercion.kwargs)

        await emit_debug_event(
            "operation_walk_forward_backtest.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        if self._platform_service is None or not hasattr(self._platform_service, "start_backtest_job"):
            return ToolResult(
                text=format_error_text("ServiceUnavailable", "backtest service is not available"),
                is_error=True,
            )

        try:
            payload = await self._run(kwargs)
        except _InvalidWalkForwardArgument as exc:
            await emit_debug_event(
                "operation_walk_forward_backtest.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(text=format_error_text(exc.error_code, str(exc), exc.hint), is_error=True)

        await emit_debug_event(
            "operation_walk_forward_backtest.validated",
            {
                "tool": self.name,
                "definition_id": payload["definition_id"],
                "status": payload["status"],
                "segments": payload["segments"],
                "eligible_windows": payload["eligible_windows"],
                "positive_windows": payload["positive_windows"],
            },
        )
        return ToolResult(text=append_json_payload(self._header(payload), payload), is_error=False)

    # ------------------------------------------------------------------
    # Core sweep
    # ------------------------------------------------------------------

    async def _run(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        definition_id = kwargs.get("definition_id")
        if not isinstance(definition_id, str) or not definition_id.strip():
            raise _InvalidWalkForwardArgument(
                "missing_definition_id", "definition_id (sd-...) is required for walk-forward"
            )
        definition_id = definition_id.strip()

        universe = kwargs.get("universe")
        if not isinstance(universe, list) or not universe:
            raise _InvalidWalkForwardArgument(
                "missing_universe",
                "universe must be a non-empty list of canonical symbols",
                "pass universe=['600519.SH', ...]",
            )

        parameters = kwargs.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise _InvalidWalkForwardArgument(
                "invalid_parameters", f"parameters must be an object, got {type(parameters).__name__}"
            )

        segments = self._resolve_int(kwargs.get("segments"), default=_DEFAULT_SEGMENTS, name="segments")
        if not (_MIN_SEGMENTS <= segments <= _MAX_SEGMENTS):
            raise _InvalidWalkForwardArgument(
                "invalid_segments", f"segments must be in [{_MIN_SEGMENTS}, {_MAX_SEGMENTS}], got {segments}"
            )
        min_trades = self._resolve_int(kwargs.get("min_trades"), default=1, name="min_trades", minimum=0)
        data_provider = kwargs.get("data_provider") or "auto"
        keep_tasks = bool(kwargs.get("keep_tasks", False))
        timeout_seconds = self._resolve_float(kwargs.get("timeout_seconds"), self._DEFAULT_TIMEOUT_SECONDS)

        d0 = self._parse_date(kwargs.get("range_start"), "range_start")
        d1 = self._parse_date(kwargs.get("range_end"), "range_end")
        if d1 < d0:
            raise _InvalidWalkForwardArgument(
                "invalid_range", f"range_end {d1.isoformat()} is before range_start {d0.isoformat()}"
            )
        if (d1 - d0).days < segments:
            raise _InvalidWalkForwardArgument(
                "range_too_short_for_segments",
                f"range spans {(d1 - d0).days} day(s) but {segments} segments requested",
                "widen the range or lower --segments",
            )

        windows = _compute_windows(d0, d1, segments)
        strategy_block: dict[str, Any] = {"definition_id": definition_id}
        if isinstance(parameters, dict) and parameters:
            strategy_block["parameter_overrides"] = dict(parameters)

        window_results: list[dict[str, Any]] = []
        for idx, (w_start, w_end) in enumerate(windows):
            await emit_debug_event(
                "operation_walk_forward_backtest.window.started",
                {"tool": self.name, "window": idx, "range_start": w_start.isoformat(), "range_end": w_end.isoformat()},
            )
            result = await self._run_window(
                idx=idx,
                w_start=w_start,
                w_end=w_end,
                definition_id=definition_id,
                strategy_block=strategy_block,
                universe=list(universe),
                data_provider=data_provider,
                keep_tasks=keep_tasks,
                timeout_seconds=timeout_seconds,
            )
            window_results.append(result)

        if all(w["status"] == "failed" for w in window_results):
            raise _InvalidWalkForwardArgument(
                "all_windows_failed",
                "every walk-forward window failed to run; see window errors",
                "check the definition compiles, universe symbols are valid, and the data source has bars for the range",
            )

        return self._verdict_payload(
            definition_id=definition_id,
            d0=d0,
            d1=d1,
            segments=segments,
            min_trades=min_trades,
            data_provider=data_provider,
            windows=window_results,
        )

    async def _run_window(
        self,
        *,
        idx: int,
        w_start: date,
        w_end: date,
        definition_id: str,
        strategy_block: dict[str, Any],
        universe: list[str],
        data_provider: str,
        keep_tasks: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        svc = self._platform_service
        base = {
            "window": idx,
            "range_start": w_start.isoformat(),
            "range_end": w_end.isoformat(),
        }
        task_id: str | None = None
        try:
            created = await svc.create_task(
                name=f"walk-forward {definition_id}@seg{idx + 1} {w_start.isoformat()}..{w_end.isoformat()}",
                mode="backtest",
                description="auto-created by walk_forward_backtest",
                data_provider=data_provider,
                settings={"strategy": dict(strategy_block), "universe": list(universe)},
            )
            task_id = getattr(created, "task_id", None)
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("create_task did not return a task_id")

            run_row = await svc.start_backtest_job(
                task_id,
                range_start=w_start.isoformat(),
                range_end=w_end.isoformat(),
                # Fast mode: a robustness sweep only needs the summary, not the
                # full debug-session spans. Keeps N sequential windows quick.
                debug_enabled=False,
            )
            run_id = run_row.get("run_id") if isinstance(run_row, dict) else None
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("start_backtest_job did not return a run_id")

            status = await self._poll_until_terminal(task_id, run_id, timeout_seconds)
            if status in _FAILED_STATUSES:
                await emit_debug_event(
                    "operation_walk_forward_backtest.window.failed",
                    {**base, "tool": self.name, "run_id": run_id, "run_status": status},
                )
                return {**base, "status": "failed", "run_id": run_id, "error": f"backtest run status={status}"}

            summary_doc = await svc.get_backtest_summary(run_id)
            summary_state = summary_doc.get("summary_state") if isinstance(summary_doc, dict) else None
            summary_json = summary_doc.get("summary") if isinstance(summary_doc, dict) else None
            if summary_state != "ok" or not isinstance(summary_json, dict):
                await emit_debug_event(
                    "operation_walk_forward_backtest.window.failed",
                    {**base, "tool": self.name, "run_id": run_id, "summary_state": summary_state},
                )
                return {
                    **base,
                    "status": "failed",
                    "run_id": run_id,
                    "error": f"summary unavailable (summary_state={summary_state})",
                }

            metrics = _extract_window_metrics(summary_json)
            await emit_debug_event(
                "operation_walk_forward_backtest.window.completed",
                {**base, "tool": self.name, "run_id": run_id, **metrics},
            )
            return {
                **base,
                "status": "ok",
                "run_id": run_id if keep_tasks else None,
                "task_id": task_id if keep_tasks else None,
                **metrics,
            }
        except _InvalidWalkForwardArgument:
            raise
        except Exception as exc:
            # A single window's failure is reported structurally and excluded
            # from the verdict — it never collapses the whole sweep. The error
            # type + message are surfaced (CLAUDE.md §错误可见性).
            logger.warning(
                "walk_forward window=%d range=%s..%s failed: %s: %s",
                idx, w_start.isoformat(), w_end.isoformat(), type(exc).__name__, exc,
            )
            await emit_debug_event(
                "operation_walk_forward_backtest.window.failed",
                {**base, "tool": self.name, "error_type": type(exc).__name__, "message": str(exc)},
            )
            return {**base, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if task_id is not None and not keep_tasks:
                try:
                    await svc.delete_task(task_id)
                except Exception as exc:
                    logger.warning(
                        "walk_forward best-effort delete_task failed task_id=%s: %s: %s",
                        task_id, type(exc).__name__, exc,
                    )

    async def _poll_until_terminal(self, task_id: str, run_id: str, timeout_seconds: float) -> str:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            row = await self._platform_service.get_backtest_job(task_id, run_id)
            status = str(row.get("status") or "") if isinstance(row, dict) else ""
            if status in _TERMINAL_STATUSES:
                return status
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"backtest run {run_id} did not finish within {timeout_seconds}s")
            await asyncio.sleep(self._POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------

    def _verdict_payload(
        self,
        *,
        definition_id: str,
        d0: date,
        d1: date,
        segments: int,
        min_trades: int,
        data_provider: str,
        windows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ok_windows = [w for w in windows if w["status"] == "ok"]
        eligible = [w for w in ok_windows if (w.get("trade_count_closed") or 0) >= max(1, min_trades)]
        for w in windows:
            w["eligible"] = w in eligible

        returns = [w["return_pct"] for w in eligible if w.get("return_pct") is not None]
        positive = sum(1 for r in returns if r > 0)
        eligible_n = len(eligible)
        failed_n = sum(1 for w in windows if w["status"] == "failed")

        if eligible_n < 2:
            status = "inconclusive"
            reason = (
                f"only {eligible_n} window(s) traded >= {max(1, min_trades)} closed trade(s); "
                "not enough out-of-sample windows to judge generalisation. "
                "Widen the range, raise turnover, or lower min_trades."
            )
        elif positive == eligible_n:
            status = "robust"
            reason = f"all {eligible_n} traded windows were profitable — the edge holds out of sample."
        else:
            status = "fragile"
            reason = (
                f"only {positive}/{eligible_n} traded windows were profitable — the backtest edge "
                "does not generalise across time (likely in-sample overfit or regime-dependent). "
                "Re-test on orthogonal windows before trusting the single-window result."
            )

        return_spread = (max(returns) - min(returns)) if returns else None
        return {
            "status": status,
            "definition_id": definition_id,
            "range_start": d0.isoformat(),
            "range_end": d1.isoformat(),
            "segments": segments,
            "min_trades": max(1, min_trades),
            "data_provider": data_provider,
            "eligible_windows": eligible_n,
            "positive_windows": positive,
            "failed_windows": failed_n,
            "return_spread_pct": round(return_spread, 4) if return_spread is not None else None,
            "verdict_reason": reason,
            "reoptimization": False,
            "note": (
                "Fixed-parameter multi-window OOS: the same parameters run on every window "
                "(no per-window re-optimisation — that needs an automated parameter search)."
            ),
            "windows": windows,
        }

    def _header(self, payload: dict[str, Any]) -> str:
        bits = [
            f"walk_forward: {payload['definition_id']} status={payload['status']}",
            f"{payload['positive_windows']}/{payload['eligible_windows']} windows profitable",
            f"segments={payload['segments']}",
        ]
        if payload["failed_windows"]:
            bits.append(f"{payload['failed_windows']} failed")
        return "; ".join(bits) + "."

    # ------------------------------------------------------------------
    # Input resolution
    # ------------------------------------------------------------------

    def _parse_date(self, value: Any, field: str) -> date:
        if not isinstance(value, str) or not value.strip():
            raise _InvalidWalkForwardArgument(
                f"missing_{field}", f"{field} must be a YYYY-MM-DD string"
            )
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise _InvalidWalkForwardArgument(
                f"invalid_{field}", f"{field}={value!r} is not a valid YYYY-MM-DD date: {exc}"
            ) from exc

    def _resolve_int(self, value: Any, *, default: int, name: str, minimum: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise _InvalidWalkForwardArgument(f"invalid_{name}", f"{name} must be an integer, got {value!r}")
        ival = int(value)
        if ival < minimum:
            raise _InvalidWalkForwardArgument(f"invalid_{name}", f"{name} must be >= {minimum}, got {ival}")
        return ival

    def _resolve_float(self, value: Any, default: float) -> float:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidWalkForwardArgument(
                "invalid_timeout_seconds", f"timeout_seconds must be a number, got {value!r}"
            )
        f = float(value)
        if f <= 0:
            raise _InvalidWalkForwardArgument("invalid_timeout_seconds", f"timeout_seconds must be > 0, got {f}")
        return f


__all__ = ["WalkForwardBacktestTool"]
