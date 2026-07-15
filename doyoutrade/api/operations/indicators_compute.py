"""``compute_indicators`` operation — compute technical indicator values.

Reads cached daily OHLCV (written by ``doyoutrade-cli data run``) from
``~/.doyoutrade/assistant/artifacts/ohlcv_<safe_code>.csv``, computes a
caller-selected set of technical indicators on it, and returns:

* the **latest value(s)** of each requested indicator (the ``tail`` rows),
* a ``report_path`` pointing at a CSV that holds the **full series** for
  every requested indicator (so downstream tools / charts can read the
  whole curve without recomputing), and
* a small summary (row counts, the indicators that ran).

Design notes (per CLAUDE.md):

* The indicator surface is an explicit **dispatch table** local to this
  tool — ``name -> callable(df, params) -> pd.Series | dict[str, pd.Series]``.
  Adding an indicator is a code change here, not a prompt change, and the
  dispatch table is the single place that knows which OHLCV columns each
  indicator needs. We never reach into ``strategy_sdk.indicators`` to make
  it "self-describe".
* Multi-output indicators (macd / bollinger / adx / kdj / keltner /
  donchian / supertrend / ichimoku) are expanded into namespaced columns
  (e.g. ``macd``, ``macd.signal``, ``macd.hist``).
* Bad ``indicators`` / ``params`` JSON, unknown indicator names, a missing
  OHLCV cache, and unknown top-level kwargs all surface as stable
  ``error_code`` tokens — never a silent skip or a generic 500.
* ``execute(**kwargs)`` runs ``_enforce_kwargs_contract`` then
  ``_apply_schema_coercion`` on entry, matching the ``stock_screen`` tool.
* Debug events use the ``operation_compute_indicators.{request, validated,
  rejected, failed, created}`` naming the rest of the operation tools use.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from doyoutrade.debug import emit_debug_event
from doyoutrade.strategy_sdk import indicators as ind
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem helpers (mirrors pattern.py)
# ---------------------------------------------------------------------------


def _get_artifacts_root() -> Path:
    return Path.home() / ".doyoutrade" / "assistant" / "artifacts"


def _safe_code(code: str) -> str:
    """Sanitize code string for use in filenames."""
    return code.replace("/", "_").replace("\\", "_").replace(":", "_")


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class _InvalidArgument(ValueError):
    """Caller-supplied parameter is structurally invalid (bad value / shape)."""

    def __init__(self, error_code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


# ---------------------------------------------------------------------------
# Per-indicator parameter coercion
# ---------------------------------------------------------------------------


def _int_param(params: dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    """Read an integer indicator parameter with a default and floor.

    Raises ``_InvalidArgument`` (``invalid_indicator_param``) instead of a
    "tolerant" ``int(value)`` truncation so a bad period surfaces rather
    than silently computing on the wrong window.
    """

    if key not in params or params[key] is None:
        return default
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidArgument(
            "invalid_indicator_param",
            f"param {key!r} must be an integer, got {type(value).__name__}({value!r})",
        )
    intval = int(value)
    if intval != float(value):
        raise _InvalidArgument(
            "invalid_indicator_param",
            f"param {key!r} must be an integer, got fractional value {value!r}",
        )
    if intval < minimum:
        raise _InvalidArgument(
            "invalid_indicator_param",
            f"param {key!r} must be >= {minimum}, got {intval}",
        )
    return intval


def _float_param(params: dict[str, Any], key: str, default: float) -> float:
    if key not in params or params[key] is None:
        return default
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidArgument(
            "invalid_indicator_param",
            f"param {key!r} must be a number, got {type(value).__name__}({value!r})",
        )
    return float(value)


# ---------------------------------------------------------------------------
# Dispatch table: name -> callable(df, params) -> Series | dict[str, Series]
#
# Each callable pulls the OHLCV columns it needs from ``df`` (which always
# carries open/high/low/close/volume) and applies any param overrides. This
# is the single source of "which indicator needs which column / param".
# ---------------------------------------------------------------------------


_C = Callable[[pd.DataFrame, dict[str, Any]], "pd.Series | dict[str, pd.Series]"]


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, df[name])


_DISPATCH: dict[str, _C] = {
    # --- close-only scalar outputs ---
    "sma": lambda df, p: ind.sma(_col(df, "close"), _int_param(p, "window", 20)),
    "ema": lambda df, p: ind.ema(_col(df, "close"), _int_param(p, "span", 20)),
    "rsi": lambda df, p: ind.rsi(_col(df, "close"), _int_param(p, "period", 14)),
    "roc": lambda df, p: ind.roc(_col(df, "close"), _int_param(p, "period", 12)),
    "momentum": lambda df, p: ind.momentum(_col(df, "close"), _int_param(p, "period", 10)),
    "trix": lambda df, p: ind.trix(_col(df, "close"), _int_param(p, "period", 15)),
    "stdev": lambda df, p: ind.stdev(_col(df, "close"), _int_param(p, "window", 20)),
    "wma": lambda df, p: ind.wma(_col(df, "close"), _int_param(p, "window", 20)),
    "dema": lambda df, p: ind.dema(_col(df, "close"), _int_param(p, "span", 20)),
    "hist_volatility": lambda df, p: ind.hist_volatility(
        _col(df, "close"),
        _int_param(p, "window", 20),
        _int_param(p, "periods_per_year", 252),
    ),
    "kama": lambda df, p: ind.kama(
        _col(df, "close"),
        _int_param(p, "period", 10),
        _int_param(p, "fast", 2),
        _int_param(p, "slow", 30),
    ),
    # --- high/low/close scalar outputs ---
    "atr": lambda df, p: ind.atr(
        _col(df, "high"), _col(df, "low"), _col(df, "close"), _int_param(p, "period", 14)
    ),
    "williams_r": lambda df, p: ind.williams_r(
        _col(df, "high"), _col(df, "low"), _col(df, "close"), _int_param(p, "period", 14)
    ),
    "cci": lambda df, p: ind.cci(
        _col(df, "high"), _col(df, "low"), _col(df, "close"), _int_param(p, "period", 20)
    ),
    "psar": lambda df, p: ind.psar(
        _col(df, "high"),
        _col(df, "low"),
        _float_param(p, "step", 0.02),
        _float_param(p, "max_step", 0.2),
    ),
    # --- volume / price-volume scalar outputs ---
    "obv": lambda df, p: ind.obv(_col(df, "close"), _col(df, "volume")),
    "ad": lambda df, p: ind.ad(
        _col(df, "high"), _col(df, "low"), _col(df, "close"), _col(df, "volume")
    ),
    "volume_ratio": lambda df, p: ind.volume_ratio(
        _col(df, "volume"), _int_param(p, "window", 20)
    ),
    "mfi": lambda df, p: ind.mfi(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _col(df, "volume"),
        _int_param(p, "period", 14),
    ),
    "vwap": lambda df, p: ind.vwap(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _col(df, "volume"),
        _int_param(p, "window", 14),
    ),
    "cmf": lambda df, p: ind.cmf(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _col(df, "volume"),
        _int_param(p, "period", 20),
    ),
    # --- multi-output (NamedTuple) -> dict ---
    "macd": lambda df, p: _macd_dict(df, p),
    "bollinger": lambda df, p: _bollinger_dict(df, p),
    "adx": lambda df, p: _adx_dict(df, p),
    "kdj": lambda df, p: _kdj_dict(df, p),
    "keltner": lambda df, p: _keltner_dict(df, p),
    "donchian": lambda df, p: _donchian_dict(df, p),
    "supertrend": lambda df, p: _supertrend_dict(df, p),
    "ichimoku": lambda df, p: _ichimoku_dict(df, p),
    "zigzag": lambda df, p: _zigzag_dict(df, p),
    "limit_up_approx": lambda df, p: _limit_up_dispatch(df, p),
    "limit_down_approx": lambda df, p: _limit_down_dispatch(df, p),
}


def _limit_board_dispatch_params(p: dict[str, Any]) -> tuple[str, float | None, float]:
    limit_pct_raw = p.get("limit_pct")
    limit_pct = float(limit_pct_raw) if limit_pct_raw is not None else None
    abs_tol = (
        float(p["abs_price_tol"])
        if p.get("abs_price_tol") is not None
        else 0.011
    )
    return str(p.get("symbol") or ""), limit_pct, abs_tol


def _limit_up_dispatch(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    symbol, limit_pct, abs_tol = _limit_board_dispatch_params(p)
    return ind.limit_up_approx(
        _col(df, "close"),
        _col(df, "high"),
        symbol=symbol,
        limit_pct=limit_pct,
        abs_price_tol=abs_tol,
    )


def _limit_down_dispatch(df: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    symbol, limit_pct, abs_tol = _limit_board_dispatch_params(p)
    return ind.limit_down_approx(
        _col(df, "close"),
        _col(df, "low"),
        symbol=symbol,
        limit_pct=limit_pct,
        abs_price_tol=abs_tol,
    )


def _macd_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.macd(
        _col(df, "close"),
        _int_param(p, "fast", 12),
        _int_param(p, "slow", 26),
        _int_param(p, "signal", 9),
    )
    return {"macd": res.macd, "signal": res.signal, "hist": res.hist}


def _bollinger_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.bollinger(
        _col(df, "close"), _int_param(p, "window", 20), _float_param(p, "num_std", 2.0)
    )
    return {"upper": res.upper, "middle": res.middle, "lower": res.lower}


def _adx_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.adx(
        _col(df, "high"), _col(df, "low"), _col(df, "close"), _int_param(p, "period", 14)
    )
    return {"adx": res.adx, "plus_di": res.plus_di, "minus_di": res.minus_di}


def _kdj_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.kdj(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _int_param(p, "n", 9),
        _int_param(p, "k_smooth", 3),
        _int_param(p, "d_smooth", 3),
    )
    return {"k": res.k, "d": res.d, "j": res.j}


def _keltner_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.keltner(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _int_param(p, "ema_window", 20),
        _int_param(p, "atr_period", 10),
        _float_param(p, "multiplier", 2.0),
    )
    return {"upper": res.upper, "middle": res.middle, "lower": res.lower}


def _donchian_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.donchian(_col(df, "high"), _col(df, "low"), _int_param(p, "window", 20))
    return {"upper": res.upper, "middle": res.middle, "lower": res.lower}


def _supertrend_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.supertrend(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _int_param(p, "period", 10),
        _float_param(p, "multiplier", 3.0),
    )
    return {"supertrend": res.supertrend, "direction": res.direction}


def _ichimoku_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.ichimoku(
        _col(df, "high"),
        _col(df, "low"),
        _col(df, "close"),
        _int_param(p, "tenkan", 9),
        _int_param(p, "kijun", 26),
        _int_param(p, "senkou_b", 52),
    )
    return {
        "tenkan": res.tenkan,
        "kijun": res.kijun,
        "senkou_a": res.senkou_a,
        "senkou_b": res.senkou_b,
        "chikou": res.chikou,
    }


def _zigzag_dict(df: pd.DataFrame, p: dict[str, Any]) -> dict[str, pd.Series]:
    res = ind.zigzag(_col(df, "close"), _float_param(p, "threshold", 0.05))
    return {"pivot": res.pivot, "direction": res.direction}


ALL_INDICATORS: tuple[str, ...] = tuple(_DISPATCH.keys())

_REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------
# Value extraction helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Convert a single cell to a JSON-safe value; NaN -> None."""

    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _tail_values(series: pd.Series, tail: int) -> list[Any]:
    """Return the last ``tail`` values of *series* as JSON-safe scalars."""

    sliced = series.tail(tail)
    return [_jsonable(v) for v in sliced.tolist()]


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class IndicatorComputeTool(OperationHandler):
    """Compute technical indicator values on cached OHLCV.

    See the module docstring for the design contract. Per CLAUDE.md
    §Assistant 工具入参规范, ``execute`` runs ``_enforce_kwargs_contract``
    then ``_apply_schema_coercion`` on entry.
    """

    name = "compute_indicators"
    description = (
        "Compute technical indicator values on cached OHLCV. Reads from "
        "~/.doyoutrade/assistant/artifacts/ohlcv_{code}.csv (run `doyoutrade-cli "
        "data run <code>` first to populate it). Returns the latest value(s) of each requested "
        "indicator and writes the full series to indicators_{code}.csv. "
        "Supports the strategy SDK indicator set (sma/ema/rsi/atr/macd/"
        "bollinger/adx/kdj/... — 'all' computes every available indicator)."
    )
    category = "analysis"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Symbol code (reads from ohlcv_{code}.csv).",
            },
            "indicators": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Indicator names, or the literal string "all" (default). '
                    "Also accepts a comma-separated string or JSON array string."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    'Per-indicator parameter overrides, e.g. '
                    '{"rsi": {"period": 21}, "kdj": {"n": 9}}.'
                ),
            },
            "tail": {
                "type": "integer",
                "description": "Number of trailing rows to return per indicator (1 = latest snapshot).",
                "default": 1,
                "minimum": 1,
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    # ``indicators`` may arrive as a JSON array string / comma string;
    # ``params`` may arrive as a JSON object string. Tolerate both shapes.
    coercion_rules = (
        SchemaCoercion(
            field="indicators",
            declared_type="array",
            item_type=str,
            error_code="invalid_indicators_json",
        ),
        SchemaCoercion(
            field="params",
            declared_type="object",
            error_code="invalid_params_json",
        ),
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        # 1. Kwargs contract — reject typos / unknown top-level keys.
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}."
                f"{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            if contract.error_kind == "unknown_arguments":
                text = format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = format_error_text(
                    "validation_error",
                    str(
                        contract.error.get("message")
                        or contract.error.get("error")
                        or "validation failed"
                    ),
                )
            return ToolResult(text=text, is_error=True)
        kwargs = dict(contract.kwargs)

        await emit_debug_event(
            f"operation_{self.name}.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        # 2. Schema coercion for indicators (array) / params (object).
        #    The CLI sends ``indicators`` as a bare string ("all" / "kdj,cci" /
        #    a JSON-array string). The array coercion would reject the comma
        #    form, so pull any string out before coercion and let
        #    ``_resolve_indicators`` normalize every string shape itself.
        indicators_raw = kwargs.get("indicators")
        indicators_is_string = isinstance(indicators_raw, str)
        all_indicators_requested = (
            indicators_is_string and indicators_raw.strip().lower() == "all"
        )
        if indicators_is_string:
            kwargs.pop("indicators", None)

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {"tool": self.name, **coercion.error},
            )
            err = coercion.error
            return ToolResult(
                text=format_error_text(
                    str(err.get("error_code") or "validation_error"),
                    str(err.get("error") or "invalid input"),
                    err.get("hint") if isinstance(err.get("hint"), str) else None,
                ),
                is_error=True,
            )
        kwargs = dict(coercion.kwargs)
        if indicators_is_string and not all_indicators_requested:
            # Restore the raw string for _resolve_indicators to normalize.
            kwargs["indicators"] = indicators_raw

        # 3. Resolve structural inputs.
        try:
            code = self._require_code(kwargs.get("code"))
            selected = self._resolve_indicators(
                kwargs.get("indicators"), all_indicators_requested
            )
            params = self._resolve_params(kwargs.get("params"))
            tail = self._resolve_tail(kwargs.get("tail"))
        except _InvalidArgument as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            extra: dict[str, Any] = {}
            if exc.error_code == "unknown_indicator":
                extra["available"] = list(ALL_INDICATORS)
            text = format_error_text(exc.error_code, str(exc), exc.hint)
            if extra.get("available"):
                text += f"\nAvailable: {', '.join(extra['available'])}"
            return ToolResult(text=text, is_error=True)

        # 4. Load cached OHLCV.
        root = _get_artifacts_root()
        safe = _safe_code(code)
        csv_path = root / f"ohlcv_{safe}.csv"
        if not csv_path.exists():
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": "ohlcv_csv_missing",
                    "code": code,
                    "path": str(csv_path),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "ohlcv_csv_missing",
                    f"OHLCV cache not found for {code} at {csv_path}.",
                    "run `doyoutrade-cli data run <code>` first to populate the OHLCV cache",
                ),
                is_error=True,
            )

        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        except Exception as exc:
            logger.warning(
                "compute_indicators failed to read OHLCV code=%s path=%s err=%s",
                code, csv_path, exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": "ohlcv_csv_read_failed",
                    "error_type": type(exc).__name__,
                    "code": code,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "ohlcv_csv_read_failed", f"failed to read OHLCV CSV: {exc}"
                ),
                is_error=True,
            )

        if df.empty:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {"tool": self.name, "error_code": "ohlcv_csv_empty", "code": code},
            )
            return ToolResult(
                text=format_error_text("ohlcv_csv_empty", f"OHLCV CSV for {code} is empty."),
                is_error=True,
            )

        missing_cols = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing_cols:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": "ohlcv_columns_missing",
                    "code": code,
                    "missing": missing_cols,
                },
            )
            return ToolResult(
                text=format_error_text(
                    "ohlcv_columns_missing",
                    f"OHLCV CSV for {code} is missing columns: {missing_cols}",
                    "re-fetch via `doyoutrade-cli data run <code>`",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.validated",
            {
                "tool": self.name,
                "code": code,
                "indicators": list(selected),
                "tail": tail,
                "bars": int(len(df)),
            },
        )

        # Inject symbol for board-specific limit pct when not overridden.
        for board_name in ("limit_up_approx", "limit_down_approx"):
            if board_name in selected:
                board_params = dict(params.get(board_name) or {})
                board_params.setdefault("symbol", code)
                params = {**params, board_name: board_params}

        # 5. Compute.
        try:
            series_map, latest = self._compute(df, selected, params, tail)
        except _InvalidArgument as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "code": code,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )
        except Exception as exc:
            logger.exception(
                "compute_indicators failed code=%s indicators=%s: %s",
                code, list(selected), exc,
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "error_code": "indicator_computation_failed",
                    "error_type": type(exc).__name__,
                    "code": code,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "indicator_computation_failed",
                    f"indicator computation raised: {exc}",
                    "check the param overrides match the indicator signature",
                ),
                is_error=True,
            )

        # 6. Write full series CSV.
        report_path = root / f"indicators_{safe}.csv"
        out_df = pd.DataFrame(series_map, index=df.index)
        root.mkdir(parents=True, exist_ok=True)
        out_df.to_csv(report_path, index=True, index_label="date")

        payload = {
            "status": "ok",
            "code": code,
            "indicators": list(selected),
            "tail": tail,
            "bars": int(len(df)),
            "columns": list(series_map.keys()),
            "latest": latest,
            "report_path": str(report_path),
        }
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                "tool": self.name,
                "code": code,
                "indicators": list(selected),
                "columns": list(series_map.keys()),
                "report_path": str(report_path),
            },
        )
        header = (
            f"Computed {len(selected)} indicator(s) for {code} over "
            f"{len(df)} bars (tail={tail}); full series at {report_path}."
        )
        return ToolResult(text=append_json_payload(header, payload))

    # ------------------------------------------------------------------
    # Input resolution
    # ------------------------------------------------------------------

    def _require_code(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise _InvalidArgument(
                "missing_code",
                f"code is required and must be a non-empty string, got {value!r}",
                hint="pass a canonical symbol, e.g. 600519.SH",
            )
        return value.strip()

    def _resolve_indicators(
        self, value: Any, all_requested: bool
    ) -> tuple[str, ...]:
        if all_requested or value is None:
            return ALL_INDICATORS
        # ``value`` may be a list[str] (coerced) or a bare string the CLI
        # passes through: a JSON-array string ('["kdj","cci"]') or a
        # comma-separated string ("kdj,cci"). Try JSON first, fall back to a
        # comma split so neither shape is mangled.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("[") or text.startswith("{"):
                # Looks like JSON intent — must parse to a list.
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise _InvalidArgument(
                        "invalid_indicators_json",
                        f"indicators looked like JSON but failed to parse: {exc}",
                    ) from exc
                if not isinstance(parsed, list):
                    raise _InvalidArgument(
                        "invalid_indicators_json",
                        f"indicators JSON must be an array, got {type(parsed).__name__}",
                    )
                names = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                # Plain comma-separated string — the form the CLI sends.
                names = [item.strip() for item in text.split(",") if item.strip()]
        elif isinstance(value, list):
            names = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise _InvalidArgument(
                "invalid_indicators_json",
                f"indicators must be an array of strings or 'all', got {type(value).__name__}",
            )
        if not names:
            return ALL_INDICATORS
        unknown = [n for n in names if n not in _DISPATCH]
        if unknown:
            raise _InvalidArgument(
                "unknown_indicator",
                f"unknown indicator(s): {unknown}",
                hint="drop unknown names; see the available list (snake_case)",
            )
        # de-dup preserving order
        return tuple(dict.fromkeys(names))

    def _resolve_params(self, value: Any) -> dict[str, dict[str, Any]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _InvalidArgument(
                "invalid_params_json",
                f"params must be an object keyed by indicator name, got {type(value).__name__}",
            )
        out: dict[str, dict[str, Any]] = {}
        for key, sub in value.items():
            if sub is None:
                continue
            if not isinstance(sub, dict):
                raise _InvalidArgument(
                    "invalid_params_json",
                    f"params[{key!r}] must be an object of overrides, got {type(sub).__name__}",
                )
            out[str(key)] = dict(sub)
        return out

    def _resolve_tail(self, value: Any) -> int:
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidArgument(
                "invalid_tail",
                f"tail must be an integer, got {type(value).__name__}({value!r})",
            )
        intval = int(value)
        if intval != float(value):
            raise _InvalidArgument(
                "invalid_tail",
                f"tail must be an integer, got fractional value {value!r}",
            )
        if intval < 1:
            raise _InvalidArgument(
                "invalid_tail",
                f"tail must be >= 1, got {intval}",
            )
        return intval

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        df: pd.DataFrame,
        selected: tuple[str, ...],
        params: dict[str, dict[str, Any]],
        tail: int,
    ) -> tuple[dict[str, pd.Series], dict[str, Any]]:
        """Run each indicator; return ``(full_series_map, latest_values)``.

        ``full_series_map`` maps each output column (namespaced for
        multi-output indicators, e.g. ``macd.signal``) to its full Series.
        ``latest`` maps the same columns to the last ``tail`` JSON-safe
        values.
        """

        series_map: dict[str, pd.Series] = {}
        latest: dict[str, Any] = {}
        for name in selected:
            fn = _DISPATCH[name]
            indicator_params = params.get(name, {})
            result = fn(df, indicator_params)
            if isinstance(result, dict):
                for sub_name, series in result.items():
                    column = f"{name}.{sub_name}"
                    series_map[column] = series
                    latest[column] = _tail_values(series, tail)
            else:
                series_map[name] = result
                latest[name] = _tail_values(result, tail)
        return series_map, latest


__all__ = ["IndicatorComputeTool", "ALL_INDICATORS"]
