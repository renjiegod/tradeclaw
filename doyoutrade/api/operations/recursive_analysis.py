"""``validate_recursive`` operation — startup_history / recursive-formula
stability check for a strategy's indicators.

Borrowed (in spirit) from freqtrade's ``recursive-analysis`` and adapted to
doyoutrade's compile → validate → smoke-test convention. The problem it
solves: a recursive indicator (EMA, Wilder-RSI, ADX, ATR, MACD, …) keeps
"warming up" for many bars. An LLM-authored strategy can declare
``startup_history = 30`` while internally relying on an ``ema(close, 60)``
that needs hundreds of bars to converge. The backtest (which feeds the
full window) then looks fine, but the live cron path — which only fetches
``startup_history`` bars — computes a *different* last-row indicator value,
so the backtest's edge silently fails to transfer to live.

This tool quantifies that drift. For one symbol it:

1. compiles the strategy (reusing ``StrategyCompiler.validate_directory``);
2. fetches a long "reference" window of real OHLCV bars ending ``as_of``;
3. runs ``populate_indicators`` at several *tail-sliced* history lengths
   (the "ladder") plus the full reference window;
4. compares the **last-row** value of every indicator column the strategy
   added against the reference value and reports the percent drift.

A column whose drift at the declared ``startup_history`` exceeds
``threshold_pct`` (or is still NaN there) is flagged ``unstable`` and the
tool recommends the smallest ladder length where every indicator converges.

The analysis itself succeeding but *finding* instability is a finding, not
a tool error: the envelope stays ``is_error=False`` with
``status="unstable"`` so the full per-column table is readable. The CLI
maps ``status="unstable"`` to a non-zero exit so it can act as a
pre-promotion gate, mirroring ``sdk validate``. Genuine failures (bad
input, compile error, no data) return ``is_error=True`` with a stable
``error_code``.

Debug events: ``operation_validate_recursive.{request, rejected, failed,
validated}`` — ``.rejected`` for the kwargs contract, ``.failed`` for
validation / compile / data errors, ``.validated`` on a completed
analysis (carrying the stable/unstable verdict).
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

_BASE_OHLCV_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "amount"})
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SUPPORTED_DATA_SOURCES = ("auto", "qmt", "akshare", "tushare", "baostock", "mootdx")

# Below this absolute reference value the percent-drift denominator is
# numerically meaningless, so we fall back to an absolute-difference test.
_NEAR_ZERO_EPSILON = 1e-9
# Default drift tolerance: a recursive indicator within 1% of its
# fully-warmed value is "converged enough" for the live path to trust.
_DEFAULT_THRESHOLD_PCT = 1.0


class _InvalidRecursiveArgument(ValueError):
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


def _jsonable_float(value: Any) -> float | None:
    """Coerce a numeric cell to a JSON-safe float, mapping NaN/inf to None."""

    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _last_indicator_values(populated: pd.DataFrame) -> dict[str, float | None]:
    """Last-row value of every numeric, non-OHLCV column the strategy added.

    Non-numeric columns (string/bool signal flags) are skipped — recursive
    drift is only meaningful for continuous indicator series. Returns
    ``{column: float | None}`` where ``None`` means the cell was NaN/inf
    (i.e. the indicator had not warmed up at that history length).
    """

    if populated.empty:
        return {}
    last = populated.iloc[-1]
    out: dict[str, float | None] = {}
    for column in populated.columns:
        name = str(column)
        if name.lower() in _BASE_OHLCV_COLUMNS:
            continue
        series = populated[column]
        if not pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            continue
        out[name] = _jsonable_float(last[column])
    return out


def _drift_pct(reference: float | None, candidate: float | None) -> tuple[float | None, bool, str | None]:
    """Compare a candidate indicator value to the fully-warmed reference.

    Returns ``(drift_pct, converged, note)``:

    * ``reference is None`` (NaN even at full history) → can't assess; the
      indicator never warmed up in the fetched window. ``note="reference_nan"``.
    * ``candidate is None`` (NaN at this short history) → not converged;
      the strategy needs more bars than this. ``note="not_warmed"``.
    * near-zero reference → fall back to absolute difference vs epsilon.
    * otherwise → ``abs(candidate - reference) / abs(reference) * 100``.
    """

    if reference is None:
        return None, False, "reference_nan"
    if candidate is None:
        return None, False, "not_warmed"
    if abs(reference) < _NEAR_ZERO_EPSILON:
        if abs(candidate - reference) < _NEAR_ZERO_EPSILON:
            return 0.0, True, "near_zero_reference"
        return None, False, "near_zero_reference"
    drift = abs(candidate - reference) / abs(reference) * 100.0
    return round(drift, 4), None, None


def _default_ladder(declared: int, reference_rows: int) -> list[int]:
    """Build the default set of tail-history lengths to test.

    Always includes the declared ``startup_history`` (the value the live
    path will actually feed). Adds 1.5×/2×/3× multiples capped below the
    reference window so each ladder point has a more-warmed baseline to
    compare against.
    """

    candidates = {
        declared,
        int(round(declared * 1.5)),
        declared * 2,
        declared * 3,
    }
    ladder = sorted(h for h in candidates if 1 <= h < reference_rows)
    if declared < reference_rows and declared not in ladder:
        ladder.append(declared)
        ladder.sort()
    return ladder


class ValidateRecursiveStabilityTool(OperationHandler):
    name = "validate_recursive"
    description = (
        "Quantify how much a strategy's indicators drift with startup_history. "
        "Compiles the strategy, fetches a long reference window of real OHLCV "
        "for one symbol, runs populate_indicators at several tail-sliced "
        "history lengths, and reports the last-row percent drift of each "
        "indicator vs its fully-warmed value. Flags columns that haven't "
        "converged at the declared startup_history (the value the live cron "
        "path feeds) and recommends a safe startup_history. status='unstable' "
        "when any indicator drifts beyond threshold_pct."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "source_code": {
                "type": "string",
                "description": "Strategy source defining a class named Strategy.",
            },
            "symbol": {
                "type": "string",
                "description": "Canonical CODE.EXCHANGE to compute indicators against.",
            },
            "as_of": {
                "type": "string",
                "description": "YYYY-MM-DD end of the reference window. Omit for latest.",
            },
            "ladder": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Tail-history lengths to test. Omit for auto (declared, 1.5x, 2x, 3x).",
            },
            "freq": {"type": "string", "default": "1d"},
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_DATA_SOURCES),
                "default": "auto",
            },
            "threshold_pct": {
                "type": "number",
                "minimum": 0.0,
                "description": "Drift tolerance in percent (default 1.0).",
            },
        },
        "additionalProperties": False,
        "required": ["source_code", "symbol"],
    }

    coercion_rules = (
        SchemaCoercion(
            field="ladder",
            declared_type="array",
            item_type=int,
            error_code="invalid_ladder_json",
        ),
    )

    def __init__(self, *, compiler: Any | None = None) -> None:
        # Default to a fresh compiler so the tool works in minimal setups
        # (mirrors how the CLI's in-process ``sdk validate`` builds one).
        if compiler is None:
            from doyoutrade.strategy_runtime.compiler import StrategyCompiler

            compiler = StrategyCompiler()
        self._compiler = compiler

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_validate_recursive.rejected",
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

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            err = coercion.error
            await emit_debug_event("operation_validate_recursive.failed", {"tool": self.name, **err})
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
            "operation_validate_recursive.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            payload = await self._run(kwargs)
        except _InvalidRecursiveArgument as exc:
            await emit_debug_event(
                "operation_validate_recursive.failed",
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
            "operation_validate_recursive.validated",
            {
                "tool": self.name,
                "symbol": payload["symbol"],
                "status": payload["status"],
                "declared_startup_history": payload["declared_startup_history"],
                "recommended_startup_history": payload["recommended_startup_history"],
                "unstable_columns": payload["unstable_columns"],
            },
        )
        return ToolResult(text=append_json_payload(self._header(payload), payload), is_error=False)

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    async def _run(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        source_code = kwargs.get("source_code")
        if not isinstance(source_code, str) or not source_code.strip():
            raise _InvalidRecursiveArgument(
                "missing_source_code", "source_code must be a non-empty string"
            )

        symbol = kwargs.get("symbol")
        if not isinstance(symbol, str) or not _SYMBOL_PATTERN.match(symbol.strip()):
            raise _InvalidRecursiveArgument(
                "invalid_symbol",
                f"symbol must be a canonical CODE.EXCHANGE string, got {symbol!r}",
                "resolve via doyoutrade-cli stock lookup first",
            )
        symbol = symbol.strip()

        freq = kwargs.get("freq") or "1d"
        data_source = kwargs.get("data_source") or "auto"
        if data_source not in _SUPPORTED_DATA_SOURCES:
            raise _InvalidRecursiveArgument(
                "unknown_data_source",
                f"unknown data_source {data_source!r}",
                f"use one of: {', '.join(_SUPPORTED_DATA_SOURCES)}",
            )
        threshold_pct = self._resolve_threshold(kwargs.get("threshold_pct"))
        as_of_date = self._resolve_as_of(kwargs.get("as_of"))

        # 1. Compile the strategy via the same path ``sdk validate`` uses.
        strategy_class, declared = self._compile(source_code)

        # 2. Fetch a long reference window of real bars.
        reference_target = max(declared * 4, 200)
        full_df = await self._fetch_reference_bars(
            symbol=symbol,
            as_of_date=as_of_date,
            reference_rows=reference_target,
            freq=freq,
            data_source=data_source,
        )
        available = int(len(full_df))
        if available < declared:
            raise _InvalidRecursiveArgument(
                "insufficient_history",
                (
                    f"only {available} bars available for {symbol} ending "
                    f"{as_of_date.isoformat()}, but startup_history={declared} bars "
                    "are required to even warm up"
                ),
                "widen as_of / pick a more liquid symbol, or lower startup_history",
            )

        reference_rows = available
        ladder = self._resolve_ladder(kwargs.get("ladder"), declared, reference_rows)

        # 3. Reference (fully-warmed) last-row indicator values.
        reference_values = self._populate_last_values(
            strategy_class, full_df.tail(reference_rows), symbol
        )
        if not reference_values:
            # populate_indicators added no numeric indicator columns — there is
            # nothing recursive to drift. Report explicitly rather than
            # silently returning "stable".
            return self._empty_indicator_payload(
                symbol=symbol,
                as_of_date=as_of_date,
                declared=declared,
                reference_rows=reference_rows,
                ladder=ladder,
                threshold_pct=threshold_pct,
                freq=freq,
            )

        # 4. Per-ladder drift vs reference.
        indicators_report: dict[str, dict[str, Any]] = {
            col: {"reference_value": ref, "by_history": {}} for col, ref in reference_values.items()
        }
        for h in ladder:
            candidate_values = self._populate_last_values(strategy_class, full_df.tail(h), symbol)
            for col, ref in reference_values.items():
                drift, converged_flag, note = _drift_pct(ref, candidate_values.get(col))
                entry: dict[str, Any] = {"value": candidate_values.get(col), "drift_pct": drift}
                if note is not None:
                    entry["note"] = note
                indicators_report[col]["by_history"][str(h)] = entry

        return self._verdict_payload(
            symbol=symbol,
            as_of_date=as_of_date,
            declared=declared,
            reference_rows=reference_rows,
            ladder=ladder,
            threshold_pct=threshold_pct,
            freq=freq,
            indicators_report=indicators_report,
        )

    def _compile(self, source_code: str) -> tuple[type, int]:
        """Compile + smoke the source (single ``strategy.py``); return (class, startup).

        Runs the same two-stage gate as ``sdk validate``: ``validate_directory``
        (AST + exec) catches static violations, then ``smoke_test`` catches
        runtime failures the AST can't see — a missing ``on_bar`` (abstract
        class), an ``__init__`` that rejects its own defaults, or
        populate/on_bar raising on synthetic data. Without the smoke stage a
        non-runnable strategy would blow up later with a bare ``TypeError``
        instead of a structured ``error_code``.
        """

        with tempfile.TemporaryDirectory(prefix="doyoutrade_validate_recursive_") as tmpdir:
            (Path(tmpdir) / "strategy.py").write_text(source_code, encoding="utf-8")
            result = self._compiler.validate_directory(Path(tmpdir))
            if not result.ok or result.artifact is None:
                # Surface the compiler's own stable error_code so the agent can
                # route the fix the same way it would for ``sdk validate``.
                raise _InvalidRecursiveArgument(
                    result.error_code or "compile_failed",
                    "; ".join(result.errors) or "strategy failed to compile",
                    "fix the compile error (run `doyoutrade-cli sdk validate` for the full report)",
                )
            smoke = self._compiler.smoke_test(result.artifact)
        if not smoke.success:
            raise _InvalidRecursiveArgument(
                smoke.error_code or "runtime_smoke_failed",
                smoke.error_message or "strategy compiled but failed the runtime smoke test",
                "run `doyoutrade-cli sdk validate` for the full compile+smoke report",
                error_type=smoke.error_type,
            )
        strategy_class = result.artifact.strategy_class
        declared = max(1, int(getattr(strategy_class, "startup_history", 30)))
        return strategy_class, declared

    async def _fetch_reference_bars(
        self,
        *,
        symbol: str,
        as_of_date: date,
        reference_rows: int,
        freq: str,
        data_source: str,
    ) -> pd.DataFrame:
        from doyoutrade.api.operations.market_data import MarketDataFetcher

        # Calendar span ≈ rows * 7/5 trading-day inflation + a generous buffer
        # for holidays / suspensions so the tail slice still has ``reference_rows``.
        calendar_days = int(math.ceil(reference_rows * 7 / 5)) + 30
        start_date = as_of_date - timedelta(days=calendar_days)
        fetcher = MarketDataFetcher()
        try:
            raw = await fetcher._fetch_ohlcv(
                symbol,
                start_dt=start_date,
                end_dt=as_of_date,
                period_label=f"{start_date.isoformat()}..{as_of_date.isoformat()}",
                interval=freq,
                data_source=data_source,
            )
        except Exception as exc:
            logger.warning(
                "validate_recursive fetch failed symbol=%s data_source=%s interval=%s err=%s",
                symbol, data_source, freq, exc,
            )
            raise _InvalidRecursiveArgument(
                "data_fetch_failed",
                f"failed to fetch OHLCV for {symbol}: {exc}",
                "check the symbol, as_of date, and data_source",
                error_type=type(exc).__name__,
            ) from exc
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise _InvalidRecursiveArgument(
                "no_bars",
                f"no bars returned for {symbol} ending {as_of_date.isoformat()}",
                "check the symbol and date range against the provider calendar",
            )
        df = raw.copy()
        df.index = pd.to_datetime(df.index)
        return df.sort_index()

    def _populate_last_values(
        self, strategy_class: type, df: pd.DataFrame, symbol: str
    ) -> dict[str, float | None]:
        """Instantiate the strategy, run populate_indicators, capture last row.

        A fresh instance per call keeps any per-instance state from leaking
        between ladder lengths. ``ctx`` is the same no-op-dp smoke context the
        compiler's smoke test uses; a strategy that reaches for ``ctx.dp``
        inside populate_indicators surfaces as a clear, typed error rather than
        a silent miscompare.
        """

        from doyoutrade.strategy_runtime.compiler import _make_smoke_context

        last_ts = df.index[-1] if len(df) else datetime(2026, 1, 1)
        ctx = _make_smoke_context(smoke_symbol=symbol, as_of=last_ts)
        strategy = strategy_class()
        try:
            populated = strategy.populate_indicators(df.copy(), ctx)
        except RuntimeError as exc:
            if "ctx.dp" in str(exc):
                raise _InvalidRecursiveArgument(
                    "populate_requires_data_provider",
                    (
                        "populate_indicators calls ctx.dp.*; recursive stability "
                        "cannot be checked offline for data-provider-dependent indicators"
                    ),
                    "move cross-symbol/data lookups out of populate_indicators, "
                    "or compute indicators purely from the bar DataFrame",
                    error_type="RuntimeError",
                ) from exc
            raise _InvalidRecursiveArgument(
                "populate_indicators_failed",
                f"populate_indicators raised RuntimeError: {exc}",
                error_type="RuntimeError",
            ) from exc
        except Exception as exc:
            raise _InvalidRecursiveArgument(
                "populate_indicators_failed",
                f"populate_indicators raised {type(exc).__name__}: {exc}",
                "the strategy compiled but failed on real bars; run `sdk validate` and check indicator math",
                error_type=type(exc).__name__,
            ) from exc
        if not isinstance(populated, pd.DataFrame):
            raise _InvalidRecursiveArgument(
                "populate_indicators_failed",
                f"populate_indicators returned {type(populated).__name__}, expected DataFrame",
            )
        return _last_indicator_values(populated)

    # ------------------------------------------------------------------
    # Verdict assembly
    # ------------------------------------------------------------------

    def _verdict_payload(
        self,
        *,
        symbol: str,
        as_of_date: date,
        declared: int,
        reference_rows: int,
        ladder: list[int],
        threshold_pct: float,
        freq: str,
        indicators_report: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        declared_key = str(declared)
        unstable_columns: list[str] = []
        for col, report in indicators_report.items():
            at_declared = report["by_history"].get(declared_key)
            drift = at_declared.get("drift_pct") if at_declared else None
            # Unstable if: declared not in ladder (no datapoint → unknown is
            # not flagged), drift exceeds threshold, or value never warmed.
            if at_declared is None:
                continue
            if drift is None or drift > threshold_pct:
                unstable_columns.append(col)
            report["stable_at_declared"] = col not in unstable_columns

        recommended = self._recommend_startup(
            indicators_report, ladder, reference_rows, threshold_pct
        )
        status = "unstable" if unstable_columns else "stable"
        return {
            "status": status,
            "symbol": symbol,
            "as_of": as_of_date.isoformat(),
            "freq": freq,
            "declared_startup_history": declared,
            "reference_history": reference_rows,
            "ladder": ladder,
            "threshold_pct": threshold_pct,
            "indicator_count": len(indicators_report),
            "unstable_columns": unstable_columns,
            "recommended_startup_history": recommended,
            "indicators": indicators_report,
        }

    def _recommend_startup(
        self,
        indicators_report: dict[str, dict[str, Any]],
        ladder: list[int],
        reference_rows: int,
        threshold_pct: float,
    ) -> int | None:
        """Smallest tested history where every indicator is within threshold.

        Falls back to ``reference_rows`` when no ladder point converges, or
        ``None`` when even the reference is NaN (the indicator never warms up
        in the fetched window — a separate problem the table surfaces).
        """

        for h in sorted(ladder):
            key = str(h)
            all_ok = True
            for report in indicators_report.values():
                entry = report["by_history"].get(key)
                if entry is None:
                    all_ok = False
                    break
                drift = entry.get("drift_pct")
                if drift is None or drift > threshold_pct:
                    all_ok = False
                    break
            if all_ok:
                return h
        # No ladder point fully converged. If the reference itself is usable
        # (no all-NaN reference), recommend the reference window length.
        if all(report.get("reference_value") is not None for report in indicators_report.values()):
            return reference_rows
        return None

    def _empty_indicator_payload(
        self,
        *,
        symbol: str,
        as_of_date: date,
        declared: int,
        reference_rows: int,
        ladder: list[int],
        threshold_pct: float,
        freq: str,
    ) -> dict[str, Any]:
        return {
            "status": "stable",
            "symbol": symbol,
            "as_of": as_of_date.isoformat(),
            "freq": freq,
            "declared_startup_history": declared,
            "reference_history": reference_rows,
            "ladder": ladder,
            "threshold_pct": threshold_pct,
            "indicator_count": 0,
            "unstable_columns": [],
            "recommended_startup_history": declared,
            "indicators": {},
            "note": (
                "populate_indicators added no numeric indicator columns; "
                "nothing recursive to assess"
            ),
        }

    def _header(self, payload: dict[str, Any]) -> str:
        bits = [
            f"validate_recursive: {payload['symbol']} status={payload['status']}",
            f"declared={payload['declared_startup_history']}",
            f"reference={payload['reference_history']} bars",
        ]
        if payload["unstable_columns"]:
            bits.append(f"unstable={','.join(payload['unstable_columns'])}")
        rec = payload.get("recommended_startup_history")
        if rec is not None and payload["status"] == "unstable":
            bits.append(f"recommend startup_history>={rec}")
        return "; ".join(bits) + "."

    # ------------------------------------------------------------------
    # Input resolution
    # ------------------------------------------------------------------

    def _resolve_threshold(self, value: Any) -> float:
        if value is None:
            return _DEFAULT_THRESHOLD_PCT
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidRecursiveArgument(
                "invalid_threshold_pct",
                f"threshold_pct must be a number >= 0, got {type(value).__name__}({value!r})",
            )
        f = float(value)
        if f < 0:
            raise _InvalidRecursiveArgument(
                "invalid_threshold_pct", f"threshold_pct must be >= 0, got {value!r}"
            )
        return f

    def _resolve_as_of(self, value: Any) -> date:
        if value is None:
            return date.today()
        if not isinstance(value, str):
            raise _InvalidRecursiveArgument(
                "invalid_as_of",
                f"as_of must be a YYYY-MM-DD string, got {type(value).__name__}({value!r})",
            )
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise _InvalidRecursiveArgument(
                "invalid_as_of", f"as_of={value!r} is not a valid YYYY-MM-DD date: {exc}"
            ) from exc

    def _resolve_ladder(self, value: Any, declared: int, reference_rows: int) -> list[int]:
        if value is None:
            return _default_ladder(declared, reference_rows)
        if not isinstance(value, list) or not value:
            raise _InvalidRecursiveArgument(
                "invalid_ladder",
                f"ladder must be a non-empty list of integers, got {value!r}",
            )
        ladder: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or int(item) != item:
                raise _InvalidRecursiveArgument(
                    "invalid_ladder",
                    f"ladder entries must be integers, got {item!r}",
                )
            h = int(item)
            if h < 1:
                raise _InvalidRecursiveArgument(
                    "invalid_ladder", f"ladder entries must be >= 1, got {h}"
                )
            if h > reference_rows:
                raise _InvalidRecursiveArgument(
                    "ladder_exceeds_reference",
                    f"ladder entry {h} exceeds available reference history {reference_rows}",
                    "use smaller history lengths or widen as_of to fetch more bars",
                )
            if h not in ladder:
                ladder.append(h)
        return sorted(ladder)


__all__ = ["ValidateRecursiveStabilityTool"]
