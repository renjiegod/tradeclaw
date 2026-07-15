"""``stock_screen`` operation — multi-symbol screener over OHLCV history.

Loops a caller-supplied universe of symbols, fetches recent bars for each via
the configured data provider, evaluates a fixed whitelist of conditions
(indicators / patterns / price-volume), and returns the symbols that match
**all** active conditions. Designed to be invoked from
``doyoutrade-cli stock screen`` and from the assistant tool registry —
no new persistence schema, no new external data source.

Design notes (per CLAUDE.md):

* The condition surface is a whitelist of named flags. There is **no**
  expression DSL: every condition the CLI accepts maps to an explicit
  schema property below. Adding a new condition is a code change, not a
  prompt change.
* Per-symbol failures (data unavailable, insufficient history, raised
  exception) are surfaced as ``screener_symbol_skipped`` debug events with
  a structured ``reason`` field and counted in the envelope's ``skipped``
  bucket. They never cause the run to abort or silently lose a symbol.
* The auto-computed lookback follows the same pattern as
  ``_compute_warmup_left_expansion_days``: enough calendar days to cover
  the widest indicator window in use, with a safety pad for holidays.
* Result CSV is written to ``~/.doyoutrade/assistant/artifacts``; the
  envelope returns ``result_path`` and a 10-row preview so the agent does
  not have to read the whole file to know what matched.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from doyoutrade.api.operations.pattern import (
    broadening,
    candlestick_patterns,
    double_top_bottom,
    head_and_shoulders,
    triangle,
)
from doyoutrade.data.local_market_bars import (
    SUPPORTED_LOCAL_INTERVALS,
    _query_bound,
    _row_to_bar,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.strategy_sdk import indicators as ind
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)

logger = logging.getLogger(__name__)


class _LocalFirstScreenProvider:
    """Screen-only read-through over the local ``market_bars`` warehouse.

    Reads already-synced bars straight from the warehouse (no network round-trip)
    and, on a local miss, falls back to the raw upstream provider **with the exact
    pre-cache call** — so a symbol absent from the warehouse screens just as it did
    before (zero regression). It NEVER persists and never runs the write-time
    continuity gate, so it cannot turn a previously-screenable symbol into a skip
    (that risk is why screen reads the warehouse instead of reusing the live
    ``LocalHistoricalBarsDataProvider`` auto-backfill stack).

    The warehouse is keyed by ``(provider, adjust, symbol, interval, timestamp)``;
    ``provider`` / ``adjust`` here MUST match what ``MarketDataSyncService`` wrote
    (``market_data.default_provider`` + that provider's
    ``capabilities.default_adjust``) — otherwise every lookup misses and the screen
    silently degrades to network-only. Hits / misses / read failures are emitted as
    debug events so the screen's session shows whether the local cache actually
    served the scan (per CLAUDE.md §最低同步要求 / §错误可见性).
    """

    def __init__(self, *, repository: Any, upstream: Any, provider: str, adjust: str) -> None:
        self._repo = repository
        self._upstream = upstream
        self._provider = provider
        self._adjust = adjust
        cap = getattr(upstream, "capabilities", None)
        if cap is not None:
            # Expose the upstream's capabilities so downstream code that probes
            # ``.capabilities`` (e.g. last_used_provider / default_adjust) sees the
            # real provider, not this thin wrapper.
            self.capabilities = cap

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str | None = None,
    ) -> list[Any]:
        resolved_adjust = adjust or self._adjust
        if interval in SUPPORTED_LOCAL_INTERVALS:
            rows: list[Any] = []
            try:
                start_bound = _query_bound(start_time, interval=interval, is_end=False)
                end_bound = _query_bound(end_time, interval=interval, is_end=True)
                rows = await self._repo.bars_in_range(
                    provider=self._provider,
                    adjust=resolved_adjust,
                    symbol=symbol,
                    interval=interval,
                    start=start_bound,
                    end=end_bound,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced + safe network fallback
                logger.warning(
                    "stock_screen local warehouse read failed symbol=%s interval=%s "
                    "provider=%s adjust=%s error_type=%s error=%s — falling back to upstream",
                    symbol, interval, self._provider, resolved_adjust,
                    type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "stock_screen.cache.read_failed",
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "provider": self._provider,
                        "adjust": resolved_adjust,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "warehouse read errored; screen fell back to upstream network fetch",
                    },
                )
                rows = []
            if rows:
                await emit_debug_event(
                    "stock_screen.cache.hit",
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "provider": self._provider,
                        "returned_count": len(rows),
                    },
                )
                return [_row_to_bar(row) for row in rows]
            await emit_debug_event(
                "stock_screen.cache.miss",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "provider": self._provider,
                    "reason": "warehouse_empty",
                    "hint": (
                        "symbol/range absent from the local market_bars warehouse; "
                        "fetching from upstream (no warehouse speedup). Warm it via "
                        "market_data.sync_full_market or 'doyoutrade-cli data sync'."
                    ),
                },
            )
        # Local miss / unsupported-local interval → the exact pre-cache network
        # path (no ``adjust`` kwarg, matching how the raw scan called it before).
        return await self._upstream.get_bars(
            symbol, start_time, end_time, interval=interval
        )

    async def aclose(self) -> None:
        close = getattr(self._upstream, "aclose", None)
        if close is not None:
            await close()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_DATA_SOURCES: tuple[str, ...] = (
    "auto",
    "qmt",
    "akshare",
    "tushare",
    "baostock",
    "mootdx",
)

# All candlestick / chart patterns the screener accepts on ``--patterns``.
# A symbol matches when ANY of the requested patterns is detected on the bar
# at or before ``asof`` (within ``pattern_window``).
_PATTERN_NAMES: tuple[str, ...] = (
    "hammer",                # candlestick bullish
    "bullish_engulfing",     # candlestick bullish
    "bearish_engulfing",     # candlestick bearish
    "doji",                  # candlestick neutral (re-detected from the same definition)
    "head_and_shoulders",
    "double_top",
    "double_bottom",
    "ascending_triangle",
    "descending_triangle",
    "broadening",
)

_MA_CROSS_RE = re.compile(r"^\s*(golden|death)\s*:\s*(\d+)\s*,\s*(\d+)\s*$", re.IGNORECASE)

_MACD_MODES: tuple[str, ...] = (
    "golden_cross",
    "death_cross",
    "cross_zero_up",
    "cross_zero_down",
)

_BOLLINGER_MODES: tuple[str, ...] = ("upper_break", "lower_break")

_KDJ_MODES: tuple[str, ...] = ("golden_cross", "death_cross")

_KELTNER_MODES: tuple[str, ...] = ("upper_break", "lower_break")

_DONCHIAN_MODES: tuple[str, ...] = ("upper_break", "lower_break")

# Metrics ``rank_by`` can compute-and-emit for ranking even when the metric
# is NOT used as a filter condition. The agent picks which one expresses
# "strongest" for its intent; the engine stays a dumb, deterministic ranker.
# Each maps to a single ``ind.*`` call with a sensible default window (or the
# matching condition window when that condition is also active).
_RANK_METRICS: tuple[str, ...] = (
    "rsi",
    "adx",
    "cci",
    "roc",
    "macd_hist",
    "avg_amount",
)

# Format for ``--ma-above-ma`` ("fast,slow", periods, fast < slow) and
# ``--ma-slope-min`` ("period,lookback,min_slope").
_MA_ABOVE_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*$")
_MA_SLOPE_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# Calendar-day → trading-day factor + safety pad. Same convention as
# ``_compute_warmup_left_expansion_days`` in cached_bars.py.
_TRADING_TO_CALENDAR_DAYS_FACTOR = 1.7
_CALENDAR_SAFETY_PAD_DAYS = 30
_MIN_LOOKBACK_CALENDAR_DAYS = 250   # ~1 year, covers all default-window indicators

# Per-symbol fetch concurrency cap. Keeps any one provider from being hit
# with hundreds of parallel requests when the universe is large.
_MAX_PARALLEL_SYMBOLS = 8


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class _InvalidArgument(ValueError):
    """Caller-supplied parameter is structurally invalid (bad value / format)."""

    def __init__(self, error_code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


class _ConflictingConditions(_InvalidArgument):
    """Two conditions cannot both hold (e.g. rsi-min 80 with rsi-max 20)."""


class _FundamentalsUnavailable(Exception):
    """A market-cap condition is active but the symbol has no fundamentals.

    Distinct from insufficient OHLCV history: the bars may be fine but the
    fundamentals provider didn't serve this symbol (e.g. not in the snapshot,
    or float_mv missing). The scan maps it to a ``fundamentals_unavailable``
    skip reason rather than silently treating market cap as zero / passing.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.symbol = symbol


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _get_artifacts_root() -> Path:
    return Path.home() / ".doyoutrade" / "assistant" / "artifacts"


def _default_result_path(asof: date) -> Path:
    root = _get_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / f"screener_{asof.isoformat()}_{ts}.csv"


# ---------------------------------------------------------------------------
# Compiled condition spec — built once from raw kwargs and reused per symbol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CompiledConditions:
    """All active screener conditions resolved into typed fields.

    ``None`` for a field means "not active". The screener evaluates each
    non-None field in turn; a symbol matches when every active condition
    is satisfied.
    """

    patterns: tuple[str, ...] | None
    pattern_window: int
    rsi_period: int
    rsi_min: float | None
    rsi_max: float | None
    ma_cross_mode: str | None        # "golden" | "death"
    ma_cross_fast: int | None
    ma_cross_slow: int | None
    cross_window: int
    price_above_ma: int | None
    price_below_ma: int | None
    pct_change_lookback: int | None
    pct_change_min: float | None
    pct_change_max: float | None
    volume_ratio_lookback: int | None
    volume_ratio_min: float | None
    close_at_high_window: int | None
    close_at_low_window: int | None
    bollinger_mode: str | None
    bollinger_window: int
    adx_period: int
    adx_min: float | None
    macd_mode: str | None
    kdj_mode: str | None             # "golden_cross" | "death_cross"
    kdj_n: int
    cci_period: int
    cci_min: float | None
    cci_max: float | None
    williams_period: int
    williams_min: float | None
    williams_max: float | None
    keltner_mode: str | None         # "upper_break" | "lower_break"
    donchian_mode: str | None        # "upper_break" | "lower_break"
    donchian_window: int
    cmf_period: int
    cmf_min: float | None
    roc_period: int
    roc_min: float | None
    roc_max: float | None
    ma_above_fast: int | None
    ma_above_slow: int | None
    ma_slope_period: int | None
    ma_slope_lookback: int | None
    ma_slope_min: float | None
    avg_amount_lookback: int | None
    avg_amount_min: float | None
    min_float_mv: float | None
    max_float_mv: float | None
    exclude_suspended: bool
    limit_up_approx: bool
    limit_down_approx: bool
    rank_by: str | None
    rank_desc: bool

    def needs_fundamentals(self) -> bool:
        return self.min_float_mv is not None or self.max_float_mv is not None

    def needs_events(self) -> bool:
        return self.exclude_suspended

    def max_window(self) -> int:
        """Largest bar-window any active condition needs.

        Used to compute the auto-lookback. Conservative — includes
        every active window even when one already dominates.
        """

        windows: list[int] = []
        if self.patterns:
            # Patterns like triangle / broadening look at a rolling window
            # internally; we re-use ``pattern_window`` as the slice they
            # inspect.
            windows.append(self.pattern_window)
        if self.rsi_min is not None or self.rsi_max is not None:
            # Wilder EMA needs roughly 3× period to stabilise.
            windows.append(self.rsi_period * 3)
        if self.ma_cross_mode is not None:
            assert self.ma_cross_slow is not None
            windows.append(self.ma_cross_slow + self.cross_window)
        if self.price_above_ma is not None:
            windows.append(self.price_above_ma)
        if self.price_below_ma is not None:
            windows.append(self.price_below_ma)
        if self.pct_change_lookback is not None:
            windows.append(self.pct_change_lookback + 1)
        if self.volume_ratio_lookback is not None:
            windows.append(self.volume_ratio_lookback + 1)
        if self.close_at_high_window is not None:
            windows.append(self.close_at_high_window)
        if self.limit_up_approx or self.limit_down_approx:
            windows.append(2)
        if self.close_at_low_window is not None:
            windows.append(self.close_at_low_window)
        if self.bollinger_mode is not None:
            windows.append(self.bollinger_window)
        if self.adx_min is not None:
            windows.append(self.adx_period * 3)
        if self.macd_mode is not None:
            # Default MACD = 12/26/9 → needs roughly slow + signal bars.
            windows.append(26 + 9)
        if self.kdj_mode is not None:
            # n-bar RSV window + smoothing warm-up margin (k_smooth + d_smooth
            # default 3+3) and the cross-window lookback.
            windows.append(self.kdj_n + 6 + self.cross_window)
        if self.cci_min is not None or self.cci_max is not None:
            windows.append(self.cci_period)
        if self.williams_min is not None or self.williams_max is not None:
            windows.append(self.williams_period)
        if self.keltner_mode is not None:
            # Keltner uses fixed ema_window=20 / atr_period=10; both are EWM
            # so allow ~4× the larger span to stabilise.
            windows.append(max(20, 10) * 4)
        if self.donchian_mode is not None:
            windows.append(self.donchian_window + 1)
        if self.cmf_min is not None:
            windows.append(self.cmf_period)
        if self.roc_min is not None or self.roc_max is not None:
            windows.append(self.roc_period + 1)
        if self.ma_above_fast is not None and self.ma_above_slow is not None:
            windows.append(self.ma_above_slow)
        if self.ma_slope_period is not None and self.ma_slope_lookback is not None:
            windows.append(self.ma_slope_period + self.ma_slope_lookback)
        if self.avg_amount_lookback is not None:
            windows.append(self.avg_amount_lookback)
        if self.rank_by is not None:
            # Rank metrics reuse the relevant condition window when active;
            # cover their standalone default warm-up so a rank-only screen
            # still fetches enough history.
            windows.append(self._rank_metric_window())
        return max(windows) if windows else 0

    def _rank_metric_window(self) -> int:
        """Bar warm-up needed to compute ``rank_by`` standalone (no filter)."""

        if self.rank_by == "rsi":
            return self.rsi_period * 3
        if self.rank_by == "adx":
            return self.adx_period * 3
        if self.rank_by == "cci":
            return self.cci_period
        if self.rank_by == "roc":
            return self.roc_period + 1
        if self.rank_by == "macd_hist":
            return 26 + 9
        if self.rank_by == "avg_amount":
            return self.avg_amount_lookback or 10
        return 0

    def has_any(self) -> bool:
        return (
            self.patterns is not None
            or self.rsi_min is not None
            or self.rsi_max is not None
            or self.ma_cross_mode is not None
            or self.price_above_ma is not None
            or self.price_below_ma is not None
            or self.pct_change_lookback is not None
            or self.volume_ratio_lookback is not None
            or self.close_at_high_window is not None
            or self.close_at_low_window is not None
            or self.bollinger_mode is not None
            or self.adx_min is not None
            or self.macd_mode is not None
            or self.kdj_mode is not None
            or self.cci_min is not None
            or self.cci_max is not None
            or self.williams_min is not None
            or self.williams_max is not None
            or self.keltner_mode is not None
            or self.donchian_mode is not None
            or self.cmf_min is not None
            or self.roc_min is not None
            or self.roc_max is not None
            or (self.ma_above_fast is not None and self.ma_above_slow is not None)
            or (self.ma_slope_period is not None and self.ma_slope_lookback is not None)
            or self.avg_amount_lookback is not None
            or self.min_float_mv is not None
            or self.max_float_mv is not None
            or self.exclude_suspended
            or self.limit_up_approx
            or self.limit_down_approx
            or self.rank_by is not None
        )


# ---------------------------------------------------------------------------
# Condition compilation (kwargs → _CompiledConditions)
# ---------------------------------------------------------------------------


def _coerce_optional_int(value: Any, name: str, *, minimum: int = 1) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name} must be an integer, got {type(value).__name__}({value!r})",
        )
    intval = int(value)
    if intval != float(value):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name} must be an integer, got fractional value {value!r}",
        )
    if intval < minimum:
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name} must be >= {minimum}, got {intval}",
        )
    return intval


def _coerce_optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name} must be a number, got {type(value).__name__}({value!r})",
        )
    return float(value)


def _parse_patterns(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"patterns must be a comma-separated string, got {type(value).__name__}",
        )
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        return None
    unknown = [n for n in names if n not in _PATTERN_NAMES]
    if unknown:
        raise _InvalidArgument(
            "unknown_pattern_name",
            f"unknown pattern(s): {unknown}; supported: {list(_PATTERN_NAMES)}",
            hint="drop unknown names from --patterns; case-sensitive snake_case",
        )
    # de-dup while preserving order
    return tuple(dict.fromkeys(names))


def _parse_ma_cross(value: Any) -> tuple[str, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"ma_cross must be 'golden:fast,slow' or 'death:fast,slow', got {type(value).__name__}",
        )
    m = _MA_CROSS_RE.match(value)
    if not m:
        raise _InvalidArgument(
            "invalid_condition_value",
            f"ma_cross={value!r} must look like 'golden:20,60' or 'death:5,20'",
            hint="format is '<golden|death>:<fast>,<slow>' with fast < slow",
        )
    mode = m.group(1).lower()
    fast = int(m.group(2))
    slow = int(m.group(3))
    if fast <= 0 or slow <= 0:
        raise _InvalidArgument(
            "invalid_condition_value",
            f"ma_cross windows must be positive, got fast={fast} slow={slow}",
        )
    if fast >= slow:
        raise _InvalidArgument(
            "invalid_condition_value",
            f"ma_cross fast({fast}) must be smaller than slow({slow})",
        )
    return mode, fast, slow


def _parse_ma_above(value: Any) -> tuple[int, int] | None:
    """Parse ``--ma-above-ma`` 'fast,slow' → (fast_period, slow_period)."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_ma_above_ma",
            f"ma_above_ma must be 'fast,slow' (e.g. '20,60'), got {type(value).__name__}",
        )
    m = _MA_ABOVE_RE.match(value)
    if not m:
        raise _InvalidArgument(
            "invalid_ma_above_ma",
            f"ma_above_ma={value!r} must look like '20,60' (shorter MA above longer MA)",
            hint="format is '<fast_period>,<slow_period>' with fast < slow",
        )
    fast = int(m.group(1))
    slow = int(m.group(2))
    if fast < 2 or slow < 2:
        raise _InvalidArgument(
            "invalid_ma_above_ma",
            f"ma_above_ma periods must be >= 2, got fast={fast} slow={slow}",
        )
    if fast >= slow:
        raise _InvalidArgument(
            "invalid_ma_above_ma",
            f"ma_above_ma fast period({fast}) must be smaller than slow period({slow})",
        )
    return fast, slow


def _parse_ma_slope(value: Any) -> tuple[int, int, float] | None:
    """Parse ``--ma-slope-min`` 'period,lookback,min_slope'.

    ``min_slope`` is the minimum *relative* change of SMA(period) over
    ``lookback`` bars: ``(ma_last / ma_prev - 1)``. Scale-free, so ``0``
    means "rising" regardless of price level.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_ma_slope",
            f"ma_slope_min must be 'period,lookback,min_slope' (e.g. '20,5,0'), "
            f"got {type(value).__name__}",
        )
    m = _MA_SLOPE_RE.match(value)
    if not m:
        raise _InvalidArgument(
            "invalid_ma_slope",
            f"ma_slope_min={value!r} must look like '20,5,0' "
            "(period, lookback bars, min relative slope)",
            hint="format is '<period>,<lookback>,<min_slope>'; min_slope=0 means rising",
        )
    period = int(m.group(1))
    lookback = int(m.group(2))
    min_slope = float(m.group(3))
    if period < 2:
        raise _InvalidArgument(
            "invalid_ma_slope", f"ma_slope_min period must be >= 2, got {period}"
        )
    if lookback < 1:
        raise _InvalidArgument(
            "invalid_ma_slope", f"ma_slope_min lookback must be >= 1, got {lookback}"
        )
    return period, lookback, min_slope


def _parse_enum(value: Any, name: str, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name} must be a string, got {type(value).__name__}",
        )
    norm = value.strip().lower()
    if not norm:
        return None
    if norm not in allowed:
        raise _InvalidArgument(
            "invalid_condition_value",
            f"{name}={value!r} not in {list(allowed)}",
        )
    return norm


def _compile_conditions(kwargs: dict[str, Any]) -> _CompiledConditions:
    patterns = _parse_patterns(kwargs.get("patterns"))
    pattern_window = _coerce_optional_int(
        kwargs.get("pattern_window"), "pattern_window", minimum=2
    ) or 10

    rsi_period = _coerce_optional_int(
        kwargs.get("rsi_period"), "rsi_period", minimum=2
    ) or 14
    rsi_min = _coerce_optional_float(kwargs.get("rsi_min"), "rsi_min")
    rsi_max = _coerce_optional_float(kwargs.get("rsi_max"), "rsi_max")
    if rsi_min is not None and rsi_max is not None and rsi_min > rsi_max:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"rsi_min({rsi_min}) > rsi_max({rsi_max}); no symbol can match both",
        )

    ma_cross = _parse_ma_cross(kwargs.get("ma_cross"))
    cross_window = _coerce_optional_int(
        kwargs.get("cross_window"), "cross_window", minimum=1
    ) or 3
    ma_cross_mode = ma_cross[0] if ma_cross else None
    ma_cross_fast = ma_cross[1] if ma_cross else None
    ma_cross_slow = ma_cross[2] if ma_cross else None

    price_above_ma = _coerce_optional_int(kwargs.get("price_above_ma"), "price_above_ma", minimum=2)
    price_below_ma = _coerce_optional_int(kwargs.get("price_below_ma"), "price_below_ma", minimum=2)
    if price_above_ma is not None and price_below_ma is not None:
        raise _ConflictingConditions(
            "conflicting_conditions",
            "price_above_ma and price_below_ma are mutually exclusive",
        )

    pct_change_lookback = _coerce_optional_int(
        kwargs.get("pct_change_lookback"), "pct_change_lookback", minimum=1
    )
    pct_change_min = _coerce_optional_float(kwargs.get("pct_change_min"), "pct_change_min")
    pct_change_max = _coerce_optional_float(kwargs.get("pct_change_max"), "pct_change_max")
    if (pct_change_min is not None or pct_change_max is not None) and pct_change_lookback is None:
        raise _InvalidArgument(
            "invalid_condition_value",
            "pct_change_min/max require pct_change_lookback to be set",
        )
    if pct_change_min is not None and pct_change_max is not None and pct_change_min > pct_change_max:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"pct_change_min({pct_change_min}) > pct_change_max({pct_change_max})",
        )

    volume_ratio_lookback = _coerce_optional_int(
        kwargs.get("volume_ratio_lookback"), "volume_ratio_lookback", minimum=1
    )
    volume_ratio_min = _coerce_optional_float(kwargs.get("volume_ratio_min"), "volume_ratio_min")
    if volume_ratio_min is not None and volume_ratio_lookback is None:
        raise _InvalidArgument(
            "invalid_condition_value",
            "volume_ratio_min requires volume_ratio_lookback to be set",
        )
    if volume_ratio_min is None and volume_ratio_lookback is not None:
        # Window without threshold is not useful — surface as a structured error
        # so the agent re-sends with both.
        raise _InvalidArgument(
            "invalid_condition_value",
            "volume_ratio_lookback requires volume_ratio_min to be set",
        )

    close_at_high_window = _coerce_optional_int(
        kwargs.get("close_at_high_window"), "close_at_high_window", minimum=2
    )
    close_at_low_window = _coerce_optional_int(
        kwargs.get("close_at_low_window"), "close_at_low_window", minimum=2
    )

    bollinger_mode = _parse_enum(kwargs.get("bollinger"), "bollinger", _BOLLINGER_MODES)
    bollinger_window = _coerce_optional_int(
        kwargs.get("bollinger_window"), "bollinger_window", minimum=2
    ) or 20

    adx_period = _coerce_optional_int(
        kwargs.get("adx_period"), "adx_period", minimum=2
    ) or 14
    adx_min = _coerce_optional_float(kwargs.get("adx_min"), "adx_min")

    macd_mode = _parse_enum(kwargs.get("macd"), "macd", _MACD_MODES)

    kdj_mode = _parse_enum(kwargs.get("kdj"), "kdj", _KDJ_MODES)
    kdj_n = _coerce_optional_int(kwargs.get("kdj_n"), "kdj_n", minimum=2) or 9

    cci_period = _coerce_optional_int(
        kwargs.get("cci_period"), "cci_period", minimum=2
    ) or 20
    cci_min = _coerce_optional_float(kwargs.get("cci_min"), "cci_min")
    cci_max = _coerce_optional_float(kwargs.get("cci_max"), "cci_max")
    if cci_min is not None and cci_max is not None and cci_min > cci_max:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"cci_min({cci_min}) > cci_max({cci_max}); no symbol can match both",
        )

    williams_period = _coerce_optional_int(
        kwargs.get("williams_period"), "williams_period", minimum=2
    ) or 14
    williams_min = _coerce_optional_float(kwargs.get("williams_min"), "williams_min")
    williams_max = _coerce_optional_float(kwargs.get("williams_max"), "williams_max")
    if williams_min is not None and williams_max is not None and williams_min > williams_max:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"williams_min({williams_min}) > williams_max({williams_max}); no symbol can match both",
        )

    keltner_mode = _parse_enum(kwargs.get("keltner"), "keltner", _KELTNER_MODES)

    donchian_mode = _parse_enum(kwargs.get("donchian"), "donchian", _DONCHIAN_MODES)
    donchian_window = _coerce_optional_int(
        kwargs.get("donchian_window"), "donchian_window", minimum=2
    ) or 20

    cmf_period = _coerce_optional_int(
        kwargs.get("cmf_period"), "cmf_period", minimum=2
    ) or 20
    cmf_min = _coerce_optional_float(kwargs.get("cmf_min"), "cmf_min")

    roc_period = _coerce_optional_int(
        kwargs.get("roc_period"), "roc_period", minimum=1
    ) or 12
    roc_min = _coerce_optional_float(kwargs.get("roc_min"), "roc_min")
    roc_max = _coerce_optional_float(kwargs.get("roc_max"), "roc_max")
    if roc_min is not None and roc_max is not None and roc_min > roc_max:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"roc_min({roc_min}) > roc_max({roc_max}); no symbol can match both",
        )

    ma_above = _parse_ma_above(kwargs.get("ma_above_ma"))
    ma_above_fast = ma_above[0] if ma_above else None
    ma_above_slow = ma_above[1] if ma_above else None

    ma_slope = _parse_ma_slope(kwargs.get("ma_slope_min"))
    ma_slope_period = ma_slope[0] if ma_slope else None
    ma_slope_lookback = ma_slope[1] if ma_slope else None
    ma_slope_min = ma_slope[2] if ma_slope else None

    avg_amount_lookback = _coerce_optional_int(
        kwargs.get("avg_amount_lookback"), "avg_amount_lookback", minimum=1
    )
    avg_amount_min = _coerce_optional_float(kwargs.get("avg_amount_min"), "avg_amount_min")
    if avg_amount_min is not None and avg_amount_lookback is None:
        raise _InvalidArgument(
            "invalid_condition_value",
            "avg_amount_min requires avg_amount_lookback to be set",
        )
    if avg_amount_min is None and avg_amount_lookback is not None:
        raise _InvalidArgument(
            "invalid_condition_value",
            "avg_amount_lookback requires avg_amount_min to be set",
        )

    exclude_suspended = bool(kwargs.get("exclude_suspended") or False)
    limit_up_approx = bool(kwargs.get("limit_up_approx") or False)
    limit_down_approx = bool(kwargs.get("limit_down_approx") or False)

    min_float_mv = _coerce_optional_float(kwargs.get("min_float_mv"), "min_float_mv")
    max_float_mv = _coerce_optional_float(kwargs.get("max_float_mv"), "max_float_mv")
    if min_float_mv is not None and max_float_mv is not None and min_float_mv > max_float_mv:
        raise _ConflictingConditions(
            "conflicting_conditions",
            f"min_float_mv({min_float_mv}) > max_float_mv({max_float_mv}); no symbol can match both",
        )

    rank_by = _parse_enum(kwargs.get("rank_by"), "rank_by", _RANK_METRICS)
    if kwargs.get("rank_by") is not None and rank_by is None:
        # _parse_enum already raises on non-empty unknown; this guards the
        # whitespace-only case so the agent gets a structured rejection.
        raise _InvalidArgument(
            "invalid_rank_metric",
            f"rank_by={kwargs.get('rank_by')!r} is not a supported metric; "
            f"supported: {list(_RANK_METRICS)}",
        )
    if rank_by == "avg_amount" and avg_amount_lookback is None:
        raise _InvalidArgument(
            "invalid_rank_metric",
            "rank_by='avg_amount' needs avg_amount_lookback to define the window",
            hint="pass --avg-amount-lookback (e.g. 10) when ranking by avg_amount",
        )
    rank_order = _parse_enum(kwargs.get("rank_order"), "rank_order", ("asc", "desc"))
    # Strongest-first is the natural default for ranking.
    rank_desc = (rank_order or "desc") == "desc"

    return _CompiledConditions(
        patterns=patterns,
        pattern_window=pattern_window,
        rsi_period=rsi_period,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        ma_cross_mode=ma_cross_mode,
        ma_cross_fast=ma_cross_fast,
        ma_cross_slow=ma_cross_slow,
        cross_window=cross_window,
        price_above_ma=price_above_ma,
        price_below_ma=price_below_ma,
        pct_change_lookback=pct_change_lookback,
        pct_change_min=pct_change_min,
        pct_change_max=pct_change_max,
        volume_ratio_lookback=volume_ratio_lookback,
        volume_ratio_min=volume_ratio_min,
        close_at_high_window=close_at_high_window,
        close_at_low_window=close_at_low_window,
        bollinger_mode=bollinger_mode,
        bollinger_window=bollinger_window,
        adx_period=adx_period,
        adx_min=adx_min,
        macd_mode=macd_mode,
        kdj_mode=kdj_mode,
        kdj_n=kdj_n,
        cci_period=cci_period,
        cci_min=cci_min,
        cci_max=cci_max,
        williams_period=williams_period,
        williams_min=williams_min,
        williams_max=williams_max,
        keltner_mode=keltner_mode,
        donchian_mode=donchian_mode,
        donchian_window=donchian_window,
        cmf_period=cmf_period,
        cmf_min=cmf_min,
        roc_period=roc_period,
        roc_min=roc_min,
        roc_max=roc_max,
        ma_above_fast=ma_above_fast,
        ma_above_slow=ma_above_slow,
        ma_slope_period=ma_slope_period,
        ma_slope_lookback=ma_slope_lookback,
        ma_slope_min=ma_slope_min,
        avg_amount_lookback=avg_amount_lookback,
        avg_amount_min=avg_amount_min,
        min_float_mv=min_float_mv,
        max_float_mv=max_float_mv,
        exclude_suspended=exclude_suspended,
        limit_up_approx=limit_up_approx,
        limit_down_approx=limit_down_approx,
        rank_by=rank_by,
        rank_desc=rank_desc,
    )


# ---------------------------------------------------------------------------
# Universe validation
# ---------------------------------------------------------------------------


def _validate_universe(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise _InvalidArgument(
            "invalid_universe",
            f"universe must be a list of symbols, got {type(value).__name__}",
        )
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _InvalidArgument(
                "invalid_universe",
                f"universe entries must be strings, got {type(item).__name__}({item!r})",
            )
        s = item.strip()
        if s:
            cleaned.append(s)
    # de-dup while preserving order
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned:
        raise _InvalidArgument(
            "invalid_universe",
            "universe is empty after stripping whitespace",
            hint="pass at least one symbol via --universe-file",
        )
    return cleaned


def _parse_asof(value: Any) -> date:
    if value is None:
        return date.today()
    if not isinstance(value, str):
        raise _InvalidArgument(
            "invalid_date",
            f"asof must be a YYYY-MM-DD string, got {type(value).__name__}",
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _InvalidArgument(
            "invalid_date",
            f"asof={value!r} is not a valid YYYY-MM-DD date: {exc}",
            hint="use YYYY-MM-DD, e.g. '2026-05-26'",
        ) from exc


# ---------------------------------------------------------------------------
# Lookback computation
# ---------------------------------------------------------------------------


def _compute_lookback_days(conditions: _CompiledConditions) -> int:
    """Map the largest active condition window to a calendar-day fetch span."""

    window = conditions.max_window()
    if window <= 0:
        return _MIN_LOOKBACK_CALENDAR_DAYS
    scaled = math.ceil(window * _TRADING_TO_CALENDAR_DAYS_FACTOR)
    return max(_MIN_LOOKBACK_CALENDAR_DAYS, scaled + _CALENDAR_SAFETY_PAD_DAYS)


# ---------------------------------------------------------------------------
# Bar fetching
# ---------------------------------------------------------------------------


def _bars_to_dataframe(bars: list[Any], asof: date) -> pd.DataFrame:
    """Convert ``list[Bar]`` to a DataFrame indexed by date, truncated to <= asof."""

    if not bars:
        return pd.DataFrame()
    rows = [
        {
            "date": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            # ``amount`` (turnover in currency) is optional on the Bar model:
            # qmt/akshare/tushare/baostock populate it, but a provider that
            # omits it must surface as NaN so an avg_amount condition skips the
            # symbol with a structured reason rather than silently treating it
            # as zero turnover.
            "amount": (
                float(bar.amount)
                if getattr(bar, "amount", None) is not None
                else float("nan")
            ),
        }
        for bar in bars
    ]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = cast(pd.DataFrame, df.set_index("date").sort_index())
    # Keep bars at or before asof so the screener reflects the requested
    # decision date even when the provider returned newer rows.
    df = cast(pd.DataFrame, df[df.index <= pd.Timestamp(asof)])
    return df


# ---------------------------------------------------------------------------
# Pattern detection (re-uses helpers in pattern.py)
# ---------------------------------------------------------------------------


def _detect_recent_patterns(
    df: pd.DataFrame,
    *,
    window: int,
    requested: tuple[str, ...],
) -> tuple[bool, list[str]]:
    """Return ``(matched, hits)`` for the candlestick / chart patterns.

    Matches when ANY of ``requested`` is detected at or within ``window``
    bars before the last row. ``hits`` lists the names actually detected
    (used as the ``matched_conditions`` column for the CSV).
    """

    if df.empty:
        return False, []

    open_ = cast(pd.Series, df["open"])
    high = cast(pd.Series, df["high"])
    low = cast(pd.Series, df["low"])
    close = cast(pd.Series, df["close"])

    hits: list[str] = []

    # Candlestick directional (hammer / engulfing).
    if any(n in requested for n in ("hammer", "bullish_engulfing", "bearish_engulfing")):
        cs = candlestick_patterns(open_, high, low, close)
        tail = cs.tail(window) if len(cs) >= window else cs
        # candlestick_patterns returns 1 (bullish: hammer/engulfing_bull),
        # -1 (bearish: engulfing_bear), 0 otherwise. We can't distinguish
        # hammer vs bullish_engulfing from the merged series — surface the
        # combined hit under each requested bullish name; ditto bearish.
        if (tail == 1).any():
            for name in ("hammer", "bullish_engulfing"):
                if name in requested:
                    hits.append(name)
        if (tail == -1).any() and "bearish_engulfing" in requested:
            hits.append("bearish_engulfing")

    # Doji is a separate definition (small body relative to range).
    if "doji" in requested:
        body = (close - open_).abs()
        rng = (high - low).replace(0, np.nan)
        is_doji = body / rng < 0.10
        tail = is_doji.tail(window) if len(is_doji) >= window else is_doji
        if tail.fillna(False).any():
            hits.append("doji")

    if "head_and_shoulders" in requested:
        hs = head_and_shoulders(close, window=window)
        tail = hs.tail(window) if len(hs) >= window else hs
        if (tail == 1).any():
            hits.append("head_and_shoulders")

    if "double_top" in requested or "double_bottom" in requested:
        dtb = double_top_bottom(close, window=window)
        tail = dtb.tail(window) if len(dtb) >= window else dtb
        if (tail == 1).any() and "double_top" in requested:
            hits.append("double_top")
        if (tail == -1).any() and "double_bottom" in requested:
            hits.append("double_bottom")

    if "ascending_triangle" in requested or "descending_triangle" in requested:
        tri = triangle(close, window=window)
        tail = tri.tail(window) if len(tri) >= window else tri
        if (tail == 1).any() and "ascending_triangle" in requested:
            hits.append("ascending_triangle")
        if (tail == -1).any() and "descending_triangle" in requested:
            hits.append("descending_triangle")

    if "broadening" in requested:
        br = broadening(close, window=window)
        tail = br.tail(window) if len(br) >= window else br
        if (tail == 1).any():
            hits.append("broadening")

    # Deduplicate while preserving order of detection.
    hits = list(dict.fromkeys(hits))
    return (bool(hits), hits)


# ---------------------------------------------------------------------------
# Per-symbol evaluation
# ---------------------------------------------------------------------------


@dataclass
class _SymbolResult:
    symbol: str
    matched: bool
    columns: dict[str, Any]
    matched_conditions: list[str]


def _rank_column_name(conditions: _CompiledConditions) -> str | None:
    """Column the ``rank_by`` value is stored under (must match ``_rank_metric_value``)."""

    if conditions.rank_by is None:
        return None
    if conditions.rank_by == "adx":
        return f"adx{conditions.adx_period}"
    return conditions.rank_by


def _rank_metric_value(
    conditions: _CompiledConditions,
    df: pd.DataFrame,
    columns: dict[str, Any],
) -> tuple[str, float | None]:
    """Compute ``rank_by`` for a matched symbol → ``(column_name, value)``.

    Reuses a value already computed by an active filter condition when
    present (e.g. ranking by ``rsi`` while an RSI filter is also active),
    otherwise computes it standalone. ``value`` is ``None`` when there is
    not enough history; the caller stores it and the sort pushes such rows
    to the end so the top of the list always reflects rankable matches.
    """

    metric = conditions.rank_by
    close = cast(pd.Series, df["close"])
    high = cast(pd.Series, df["high"])
    low = cast(pd.Series, df["low"])
    n = len(df)

    if metric == "rsi":
        if "rsi" in columns:
            return "rsi", columns["rsi"]
        if n < conditions.rsi_period + 1:
            return "rsi", None
        v = ind.rsi(close, period=conditions.rsi_period).iloc[-1]
        return "rsi", (None if pd.isna(v) else float(v))
    if metric == "adx":
        key = f"adx{conditions.adx_period}"
        if key in columns:
            return key, columns[key]
        if n < conditions.adx_period * 2 + 1:
            return key, None
        v = ind.adx(high, low, close, period=conditions.adx_period).adx.iloc[-1]
        return key, (None if pd.isna(v) else float(v))
    if metric == "cci":
        if "cci" in columns:
            return "cci", columns["cci"]
        if n < conditions.cci_period:
            return "cci", None
        v = ind.cci(high, low, close, period=conditions.cci_period).iloc[-1]
        return "cci", (None if pd.isna(v) else float(v))
    if metric == "roc":
        if "roc" in columns:
            return "roc", columns["roc"]
        if n < conditions.roc_period + 1:
            return "roc", None
        v = ind.roc(close, period=conditions.roc_period).iloc[-1]
        return "roc", (None if pd.isna(v) else float(v))
    if metric == "macd_hist":
        if "macd_hist" in columns:
            return "macd_hist", columns["macd_hist"]
        if n < 26 + 9 + 1:
            return "macd_hist", None
        v = ind.macd(close).hist.iloc[-1]
        return "macd_hist", (None if pd.isna(v) else float(v))
    if metric == "avg_amount":
        if "avg_amount" in columns:
            return "avg_amount", columns["avg_amount"]
        lb = conditions.avg_amount_lookback or 10
        if n < lb:
            return "avg_amount", None
        window = cast(pd.Series, df["amount"]).iloc[-lb:]
        if bool(window.isna().any()):
            return "avg_amount", None
        return "avg_amount", float(window.mean())
    # Whitelist enforced at compile time; defensive fallthrough.
    return str(metric), None


def _evaluate_symbol(
    symbol: str,
    df: pd.DataFrame,
    conditions: _CompiledConditions,
    fundamentals: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
) -> _SymbolResult | None:
    """Apply every active condition to *df*; ``None`` means insufficient data.

    ``fundamentals`` maps canonical symbol → ``Fundamentals`` and is only
    consulted when a market-cap condition is active. A symbol missing
    fundamentals while such a condition is active raises
    :class:`_FundamentalsUnavailable` so the scan can skip it with a distinct
    reason rather than treating market cap as zero.
    """

    if df.empty or len(df) < 2:
        return None

    close = cast(pd.Series, df["close"])
    high = cast(pd.Series, df["high"])
    low = cast(pd.Series, df["low"])
    volume = cast(pd.Series, df["volume"])
    last_close = float(close.iloc[-1])
    bar_count = len(df)

    columns: dict[str, Any] = {"close": last_close, "bar_count": bar_count}
    matched_conditions: list[str] = []
    matched = True

    # --- Patterns --------------------------------------------------------
    if conditions.patterns is not None:
        if bar_count < conditions.pattern_window:
            return None
        ok, hits = _detect_recent_patterns(
            df, window=conditions.pattern_window, requested=conditions.patterns
        )
        if not ok:
            matched = False
        else:
            matched_conditions.append(f"patterns:{','.join(hits)}")

    # --- RSI -------------------------------------------------------------
    if matched and (conditions.rsi_min is not None or conditions.rsi_max is not None):
        if bar_count < conditions.rsi_period + 1:
            return None
        rsi_series = ind.rsi(close, period=conditions.rsi_period)
        rsi_value = rsi_series.iloc[-1]
        if pd.isna(rsi_value):
            return None
        rsi_value = float(rsi_value)
        columns["rsi"] = rsi_value
        if conditions.rsi_min is not None and rsi_value < conditions.rsi_min:
            matched = False
        elif conditions.rsi_max is not None and rsi_value > conditions.rsi_max:
            matched = False
        else:
            bounds = []
            if conditions.rsi_min is not None:
                bounds.append(f">={conditions.rsi_min}")
            if conditions.rsi_max is not None:
                bounds.append(f"<={conditions.rsi_max}")
            matched_conditions.append(f"rsi({conditions.rsi_period}){'/'.join(bounds)}")

    # --- MA cross --------------------------------------------------------
    if matched and conditions.ma_cross_mode is not None:
        assert conditions.ma_cross_fast is not None and conditions.ma_cross_slow is not None
        slow = conditions.ma_cross_slow
        if bar_count < slow + conditions.cross_window:
            return None
        fast_ma = ind.sma(close, conditions.ma_cross_fast)
        slow_ma = ind.sma(close, slow)
        if conditions.ma_cross_mode == "golden":
            cross = ind.crossed_above(fast_ma, slow_ma)
        else:
            cross = ind.crossed_below(fast_ma, slow_ma)
        recent = cross.tail(conditions.cross_window).fillna(False)
        if recent.any():
            matched_conditions.append(
                f"ma_cross:{conditions.ma_cross_mode}:{conditions.ma_cross_fast},{slow}"
            )
        else:
            matched = False

    # --- Price vs MA -----------------------------------------------------
    if matched and conditions.price_above_ma is not None:
        win = conditions.price_above_ma
        if bar_count < win:
            return None
        ma = ind.sma(close, win)
        ma_last = ma.iloc[-1]
        if pd.isna(ma_last):
            return None
        ma_last = float(ma_last)
        columns[f"ma{win}"] = ma_last
        if last_close > ma_last:
            matched_conditions.append(f"price_above_ma:{win}")
        else:
            matched = False
    if matched and conditions.price_below_ma is not None:
        win = conditions.price_below_ma
        if bar_count < win:
            return None
        ma = ind.sma(close, win)
        ma_last = ma.iloc[-1]
        if pd.isna(ma_last):
            return None
        ma_last = float(ma_last)
        columns[f"ma{win}"] = ma_last
        if last_close < ma_last:
            matched_conditions.append(f"price_below_ma:{win}")
        else:
            matched = False

    # --- Approximate limit-up (board pct + close == high) -----------------
    if matched and conditions.limit_up_approx:
        limit_flag = ind.limit_up_approx(close, high, symbol=symbol)
        if not bool(limit_flag.iloc[-1]):
            matched = False
        else:
            matched_conditions.append("limit_up_approx")
    if matched and conditions.limit_down_approx:
        limit_flag = ind.limit_down_approx(close, low, symbol=symbol)
        if not bool(limit_flag.iloc[-1]):
            matched = False
        else:
            matched_conditions.append("limit_down_approx")

    # --- Pct change ------------------------------------------------------
    if matched and conditions.pct_change_lookback is not None:
        lb = conditions.pct_change_lookback
        if bar_count < lb + 1:
            return None
        base = float(close.iloc[-1 - lb])
        if base == 0:
            return None
        pct = (last_close - base) / base
        columns["pct_change"] = pct
        ok = True
        if conditions.pct_change_min is not None and pct < conditions.pct_change_min:
            ok = False
        if conditions.pct_change_max is not None and pct > conditions.pct_change_max:
            ok = False
        if ok:
            bounds = []
            if conditions.pct_change_min is not None:
                bounds.append(f">={conditions.pct_change_min}")
            if conditions.pct_change_max is not None:
                bounds.append(f"<={conditions.pct_change_max}")
            matched_conditions.append(f"pct_change({lb}){'/'.join(bounds)}")
        else:
            matched = False

    # --- Volume ratio ---------------------------------------------------
    if matched and conditions.volume_ratio_lookback is not None:
        lb = conditions.volume_ratio_lookback
        if bar_count < lb + 1:
            return None
        avg_vol = float(volume.iloc[-1 - lb:-1].mean())
        last_vol = float(volume.iloc[-1])
        if avg_vol <= 0:
            return None
        ratio = last_vol / avg_vol
        columns["volume_ratio"] = ratio
        assert conditions.volume_ratio_min is not None  # enforced at compile
        if ratio >= conditions.volume_ratio_min:
            matched_conditions.append(
                f"volume_ratio({lb})>={conditions.volume_ratio_min}"
            )
        else:
            matched = False

    # --- Close at recent high / low -------------------------------------
    if matched and conditions.close_at_high_window is not None:
        win = conditions.close_at_high_window
        if bar_count < win:
            return None
        recent_high = float(close.iloc[-win:].max())
        columns[f"high{win}"] = recent_high
        if math.isclose(last_close, recent_high, rel_tol=1e-9):
            matched_conditions.append(f"close_at_high:{win}")
        else:
            matched = False
    if matched and conditions.close_at_low_window is not None:
        win = conditions.close_at_low_window
        if bar_count < win:
            return None
        recent_low = float(close.iloc[-win:].min())
        columns[f"low{win}"] = recent_low
        if math.isclose(last_close, recent_low, rel_tol=1e-9):
            matched_conditions.append(f"close_at_low:{win}")
        else:
            matched = False

    # --- Bollinger break ------------------------------------------------
    if matched and conditions.bollinger_mode is not None:
        win = conditions.bollinger_window
        if bar_count < win:
            return None
        bb = ind.bollinger(close, window=win)
        upper = bb.upper.iloc[-1]
        lower = bb.lower.iloc[-1]
        if pd.isna(upper) or pd.isna(lower):
            return None
        upper_f = float(upper)
        lower_f = float(lower)
        columns[f"bb_upper{win}"] = upper_f
        columns[f"bb_lower{win}"] = lower_f
        if conditions.bollinger_mode == "upper_break" and last_close > upper_f:
            matched_conditions.append(f"bollinger:upper_break:{win}")
        elif conditions.bollinger_mode == "lower_break" and last_close < lower_f:
            matched_conditions.append(f"bollinger:lower_break:{win}")
        else:
            matched = False

    # --- ADX -------------------------------------------------------------
    if matched and conditions.adx_min is not None:
        if bar_count < conditions.adx_period * 2 + 1:
            return None
        adx_res = ind.adx(high, low, close, period=conditions.adx_period)
        adx_value = adx_res.adx.iloc[-1]
        if pd.isna(adx_value):
            return None
        adx_value = float(adx_value)
        columns[f"adx{conditions.adx_period}"] = adx_value
        if adx_value >= conditions.adx_min:
            matched_conditions.append(f"adx({conditions.adx_period})>={conditions.adx_min}")
        else:
            matched = False

    # --- MACD -----------------------------------------------------------
    if matched and conditions.macd_mode is not None:
        if bar_count < 26 + 9 + 1:
            return None
        macd_res = ind.macd(close)
        macd_line = macd_res.macd
        signal_line = macd_res.signal
        if conditions.macd_mode == "golden_cross":
            cross = ind.crossed_above(macd_line, signal_line)
            tail = cross.tail(conditions.cross_window).fillna(False)
            ok = bool(tail.any())
        elif conditions.macd_mode == "death_cross":
            cross = ind.crossed_below(macd_line, signal_line)
            tail = cross.tail(conditions.cross_window).fillna(False)
            ok = bool(tail.any())
        elif conditions.macd_mode == "cross_zero_up":
            zero = pd.Series(0.0, index=macd_line.index)
            cross = ind.crossed_above(macd_line, zero)
            tail = cross.tail(conditions.cross_window).fillna(False)
            ok = bool(tail.any())
        else:  # cross_zero_down
            zero = pd.Series(0.0, index=macd_line.index)
            cross = ind.crossed_below(macd_line, zero)
            tail = cross.tail(conditions.cross_window).fillna(False)
            ok = bool(tail.any())
        if ok:
            matched_conditions.append(f"macd:{conditions.macd_mode}")
        else:
            matched = False

    # --- KDJ cross -------------------------------------------------------
    if matched and conditions.kdj_mode is not None:
        n = conditions.kdj_n
        # n-bar RSV window + smoothing warm-up + cross-window lookback.
        if bar_count < n + 6 + conditions.cross_window:
            return None
        kdj_res = ind.kdj(high, low, close, n=n)
        k_line = kdj_res.k
        d_line = kdj_res.d
        columns["kdj_k"] = (
            None if pd.isna(k_line.iloc[-1]) else float(k_line.iloc[-1])
        )
        columns["kdj_d"] = (
            None if pd.isna(d_line.iloc[-1]) else float(d_line.iloc[-1])
        )
        j_last = kdj_res.j.iloc[-1]
        columns["kdj_j"] = None if pd.isna(j_last) else float(j_last)
        if conditions.kdj_mode == "golden_cross":
            cross = ind.crossed_above(k_line, d_line)
        else:
            cross = ind.crossed_below(k_line, d_line)
        recent = cross.tail(conditions.cross_window).fillna(False)
        if bool(recent.any()):
            matched_conditions.append(f"kdj:{conditions.kdj_mode}:{n}")
        else:
            matched = False

    # --- CCI -------------------------------------------------------------
    if matched and (conditions.cci_min is not None or conditions.cci_max is not None):
        if bar_count < conditions.cci_period:
            return None
        cci_series = ind.cci(high, low, close, period=conditions.cci_period)
        cci_value = cci_series.iloc[-1]
        if pd.isna(cci_value):
            return None
        cci_value = float(cci_value)
        columns["cci"] = cci_value
        ok = True
        if conditions.cci_min is not None and cci_value < conditions.cci_min:
            ok = False
        if conditions.cci_max is not None and cci_value > conditions.cci_max:
            ok = False
        if ok:
            bounds = []
            if conditions.cci_min is not None:
                bounds.append(f">={conditions.cci_min}")
            if conditions.cci_max is not None:
                bounds.append(f"<={conditions.cci_max}")
            matched_conditions.append(f"cci({conditions.cci_period}){'/'.join(bounds)}")
        else:
            matched = False

    # --- Williams %R -----------------------------------------------------
    if matched and (
        conditions.williams_min is not None or conditions.williams_max is not None
    ):
        if bar_count < conditions.williams_period:
            return None
        wr_series = ind.williams_r(high, low, close, period=conditions.williams_period)
        wr_value = wr_series.iloc[-1]
        if pd.isna(wr_value):
            return None
        wr_value = float(wr_value)
        columns["williams_r"] = wr_value
        ok = True
        if conditions.williams_min is not None and wr_value < conditions.williams_min:
            ok = False
        if conditions.williams_max is not None and wr_value > conditions.williams_max:
            ok = False
        if ok:
            bounds = []
            if conditions.williams_min is not None:
                bounds.append(f">={conditions.williams_min}")
            if conditions.williams_max is not None:
                bounds.append(f"<={conditions.williams_max}")
            matched_conditions.append(
                f"williams_r({conditions.williams_period}){'/'.join(bounds)}"
            )
        else:
            matched = False

    # --- Keltner break ---------------------------------------------------
    if matched and conditions.keltner_mode is not None:
        # Keltner uses fixed ema_window=20 / atr_period=10; both EWM.
        if bar_count < max(20, 10) * 2 + 1:
            return None
        kc = ind.keltner(high, low, close)
        upper = kc.upper.iloc[-1]
        lower = kc.lower.iloc[-1]
        if pd.isna(upper) or pd.isna(lower):
            return None
        upper_f = float(upper)
        lower_f = float(lower)
        columns["keltner_upper"] = upper_f
        columns["keltner_lower"] = lower_f
        if conditions.keltner_mode == "upper_break" and last_close > upper_f:
            matched_conditions.append("keltner:upper_break")
        elif conditions.keltner_mode == "lower_break" and last_close < lower_f:
            matched_conditions.append("keltner:lower_break")
        else:
            matched = False

    # --- Donchian break --------------------------------------------------
    if matched and conditions.donchian_mode is not None:
        win = conditions.donchian_window
        # Breakout compares the decision-bar close against the channel formed
        # by the PRIOR ``win`` bars (shift by 1 so the current bar's own high /
        # low can't include itself in its own channel). Needs win + 1 bars.
        if bar_count < win + 1:
            return None
        dc = ind.donchian(high, low, window=win)
        upper = dc.upper.shift(1).iloc[-1]
        lower = dc.lower.shift(1).iloc[-1]
        if pd.isna(upper) or pd.isna(lower):
            return None
        upper_f = float(upper)
        lower_f = float(lower)
        columns["donchian_upper"] = upper_f
        columns["donchian_lower"] = lower_f
        if conditions.donchian_mode == "upper_break" and last_close >= upper_f:
            matched_conditions.append(f"donchian:upper_break:{win}")
        elif conditions.donchian_mode == "lower_break" and last_close <= lower_f:
            matched_conditions.append(f"donchian:lower_break:{win}")
        else:
            matched = False

    # --- CMF -------------------------------------------------------------
    if matched and conditions.cmf_min is not None:
        if bar_count < conditions.cmf_period:
            return None
        cmf_series = ind.cmf(high, low, close, volume, period=conditions.cmf_period)
        cmf_value = cmf_series.iloc[-1]
        if pd.isna(cmf_value):
            return None
        cmf_value = float(cmf_value)
        columns["cmf"] = cmf_value
        if cmf_value >= conditions.cmf_min:
            matched_conditions.append(f"cmf({conditions.cmf_period})>={conditions.cmf_min}")
        else:
            matched = False

    # --- ROC -------------------------------------------------------------
    if matched and (conditions.roc_min is not None or conditions.roc_max is not None):
        if bar_count < conditions.roc_period + 1:
            return None
        roc_series = ind.roc(close, period=conditions.roc_period)
        roc_value = roc_series.iloc[-1]
        if pd.isna(roc_value):
            return None
        roc_value = float(roc_value)
        columns["roc"] = roc_value
        ok = True
        if conditions.roc_min is not None and roc_value < conditions.roc_min:
            ok = False
        if conditions.roc_max is not None and roc_value > conditions.roc_max:
            ok = False
        if ok:
            bounds = []
            if conditions.roc_min is not None:
                bounds.append(f">={conditions.roc_min}")
            if conditions.roc_max is not None:
                bounds.append(f"<={conditions.roc_max}")
            matched_conditions.append(f"roc({conditions.roc_period}){'/'.join(bounds)}")
        else:
            matched = False

    # --- MA above MA (shorter MA above longer MA at asof) ----------------
    if matched and conditions.ma_above_fast is not None:
        fast = conditions.ma_above_fast
        slow = conditions.ma_above_slow
        assert slow is not None
        if bar_count < slow:
            return None
        fast_ma = ind.sma(close, fast)
        slow_ma = ind.sma(close, slow)
        f_last = fast_ma.iloc[-1]
        s_last = slow_ma.iloc[-1]
        if pd.isna(f_last) or pd.isna(s_last):
            return None
        f_last = float(f_last)
        s_last = float(s_last)
        columns[f"ma{fast}"] = f_last
        columns[f"ma{slow}"] = s_last
        if f_last > s_last:
            matched_conditions.append(f"ma_above_ma:{fast}>{slow}")
        else:
            matched = False

    # --- MA slope (relative rise of SMA over a lookback) -----------------
    if matched and conditions.ma_slope_period is not None:
        period = conditions.ma_slope_period
        lb = conditions.ma_slope_lookback
        assert lb is not None and conditions.ma_slope_min is not None
        if bar_count < period + lb:
            return None
        ma = ind.sma(close, period)
        ma_last = ma.iloc[-1]
        ma_prev = ma.iloc[-1 - lb]
        if pd.isna(ma_last) or pd.isna(ma_prev) or float(ma_prev) == 0:
            return None
        slope = float(ma_last) / float(ma_prev) - 1.0
        columns[f"ma_slope{period}"] = slope
        if slope >= conditions.ma_slope_min:
            matched_conditions.append(
                f"ma_slope({period},{lb})>={conditions.ma_slope_min}"
            )
        else:
            matched = False

    # --- Average turnover (amount) over a lookback window ----------------
    if matched and conditions.avg_amount_lookback is not None:
        lb = conditions.avg_amount_lookback
        assert conditions.avg_amount_min is not None
        if bar_count < lb:
            return None
        amount = cast(pd.Series, df["amount"])
        window = amount.iloc[-lb:]
        if bool(window.isna().any()):
            # Provider did not supply turnover for some bars in the window —
            # cannot evaluate the absolute threshold. Treat as insufficient
            # data (skipped with a structured reason) rather than coercing
            # missing turnover to zero.
            return None
        avg_amount = float(window.mean())
        columns["avg_amount"] = avg_amount
        if avg_amount >= conditions.avg_amount_min:
            matched_conditions.append(f"avg_amount({lb})>={conditions.avg_amount_min}")
        else:
            matched = False

    # --- Float market cap (fundamentals axis, not derived from bars) -----
    if matched and conditions.needs_fundamentals():
        f = (fundamentals or {}).get(symbol)
        float_mv = getattr(f, "float_mv", None) if f is not None else None
        if float_mv is None:
            # Bars may be fine, but we cannot evaluate the market-cap gate —
            # surface a distinct skip reason rather than passing or zeroing it.
            raise _FundamentalsUnavailable(symbol)
        float_mv = float(float_mv)
        columns["float_mv"] = float_mv
        ok = True
        if conditions.min_float_mv is not None and float_mv < conditions.min_float_mv:
            ok = False
        if conditions.max_float_mv is not None and float_mv > conditions.max_float_mv:
            ok = False
        if ok:
            bounds = []
            if conditions.min_float_mv is not None:
                bounds.append(f">={conditions.min_float_mv:g}")
            if conditions.max_float_mv is not None:
                bounds.append(f"<={conditions.max_float_mv:g}")
            matched_conditions.append(f"float_mv{'/'.join(bounds)}")
        else:
            matched = False

    # --- Suspension / event-risk gate (events axis) ---------------------
    if matched and conditions.exclude_suspended:
        evs = (events or {}).get(symbol) or []
        if any(getattr(e, "event_type", "") == "suspension" for e in evs):
            matched = False
        else:
            matched_conditions.append("not_suspended")

    # --- Rank metric (computed for ordering even when not a filter) ------
    if matched and conditions.rank_by is not None:
        rank_col, rank_val = _rank_metric_value(conditions, df, columns)
        columns[rank_col] = rank_val

    return _SymbolResult(
        symbol=symbol,
        matched=matched,
        columns=columns,
        matched_conditions=matched_conditions,
    )


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def _sort_results(
    rows: list[dict[str, Any]],
    *,
    sort_by: str | None,
    sort_desc: bool,
) -> list[dict[str, Any]]:
    if not rows:
        return rows
    if not sort_by or sort_by == "symbol":
        return sorted(rows, key=lambda r: r.get("symbol", ""), reverse=sort_desc)
    key = sort_by

    def _sort_key(row: dict[str, Any]) -> tuple[int, float]:
        v = row.get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            # Push NaN/missing to the end regardless of direction so the
            # top of the list always reflects the strongest matches.
            return (1, 0.0)
        try:
            return (0, float(v))
        except (TypeError, ValueError):
            return (1, 0.0)

    return sorted(rows, key=_sort_key, reverse=sort_desc)


# ---------------------------------------------------------------------------
# Result writing
# ---------------------------------------------------------------------------


def _write_result_csv(
    rows: list[dict[str, Any]],
    *,
    columns: list[str],
    output_path: Path,
) -> Path:
    if not output_path.parent.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
    # The pandas stub types ``columns=`` as ``Axes | None``; ``list[str]`` is
    # the supported runtime form (pandas iterates it directly).
    df = pd.DataFrame(rows, columns=cast(Any, columns))
    df.to_csv(output_path, index=False)
    return output_path


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class StockScreenTool(OperationHandler):
    """Multi-symbol screener over OHLCV history.

    See the module docstring for the design contract. Per CLAUDE.md
    §Assistant 工具入参规范, ``execute`` runs ``_enforce_kwargs_contract``
    on entry so unknown top-level keys produce ``unknown_arguments``
    instead of a silent skip.
    """

    name = "stock_screen"
    description = (
        "Screen a list of symbols against a whitelist of conditions "
        "(patterns / RSI / MA cross / price vs MA / pct change / volume "
        "ratio / new high or low / Bollinger / ADX / MACD / KDJ / CCI / "
        "Williams %R / Keltner / Donchian / CMF / ROC). Returns the "
        "matched symbols and writes a CSV artifact for downstream tools."
    )
    category = "analysis"
    parameters = {
        "type": "object",
        "properties": {
            "universe": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of canonical CODE.EXCHANGE symbols to scan.",
                "minItems": 1,
            },
            "asof": {
                "type": "string",
                "description": "Decision date (YYYY-MM-DD). Bars after this date are ignored.",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "interval": {
                "type": "string",
                "description": "Bar interval. v1 supports '1d' only.",
                "default": "1d",
                "enum": ["1d"],
            },
            "data_source": {
                "type": "string",
                "description": "Market data provider. Same set as data_run.",
                "default": "auto",
                "enum": list(_SUPPORTED_DATA_SOURCES),
            },
            "patterns": {
                "type": "string",
                "description": (
                    "Comma-separated pattern names. Any-of match within "
                    "pattern_window bars before asof."
                ),
            },
            "pattern_window": {"type": "integer", "default": 10, "minimum": 2},
            "rsi_period": {"type": "integer", "default": 14, "minimum": 2},
            "rsi_min": {"type": "number"},
            "rsi_max": {"type": "number"},
            "ma_cross": {
                "type": "string",
                "description": "Format 'golden:fast,slow' or 'death:fast,slow'.",
            },
            "cross_window": {"type": "integer", "default": 3, "minimum": 1},
            "price_above_ma": {"type": "integer", "minimum": 2},
            "price_below_ma": {"type": "integer", "minimum": 2},
            "pct_change_lookback": {"type": "integer", "minimum": 1},
            "pct_change_min": {"type": "number"},
            "pct_change_max": {"type": "number"},
            "volume_ratio_lookback": {"type": "integer", "minimum": 1},
            "volume_ratio_min": {"type": "number"},
            "close_at_high_window": {"type": "integer", "minimum": 2},
            "close_at_low_window": {"type": "integer", "minimum": 2},
            "bollinger": {"type": "string", "enum": list(_BOLLINGER_MODES)},
            "bollinger_window": {"type": "integer", "default": 20, "minimum": 2},
            "adx_period": {"type": "integer", "default": 14, "minimum": 2},
            "adx_min": {"type": "number"},
            "macd": {"type": "string", "enum": list(_MACD_MODES)},
            "kdj": {
                "type": "string",
                "enum": list(_KDJ_MODES),
                "description": "KDJ K/D cross within --cross-window bars.",
            },
            "kdj_n": {"type": "integer", "default": 9, "minimum": 2},
            "cci_period": {"type": "integer", "default": 20, "minimum": 2},
            "cci_min": {"type": "number"},
            "cci_max": {"type": "number"},
            "williams_period": {"type": "integer", "default": 14, "minimum": 2},
            "williams_min": {"type": "number"},
            "williams_max": {"type": "number"},
            "keltner": {
                "type": "string",
                "enum": list(_KELTNER_MODES),
                "description": "Match when close breaks Keltner upper / lower channel.",
            },
            "donchian": {
                "type": "string",
                "enum": list(_DONCHIAN_MODES),
                "description": "Match when close hits Donchian upper / lower band.",
            },
            "donchian_window": {"type": "integer", "default": 20, "minimum": 2},
            "cmf_min": {"type": "number"},
            "cmf_period": {"type": "integer", "default": 20, "minimum": 2},
            "roc_period": {"type": "integer", "default": 12, "minimum": 1},
            "roc_min": {"type": "number"},
            "roc_max": {"type": "number"},
            "ma_above_ma": {
                "type": "string",
                "description": (
                    "Match when SMA(fast) > SMA(slow) at asof. Format "
                    "'fast,slow' (e.g. '20,60'); fast period < slow period."
                ),
            },
            "ma_slope_min": {
                "type": "string",
                "description": (
                    "Match when SMA(period) relative slope over lookback bars "
                    ">= min_slope. Format 'period,lookback,min_slope' "
                    "(e.g. '20,5,0'); min_slope=0 means rising."
                ),
            },
            "avg_amount_lookback": {"type": "integer", "minimum": 1},
            "avg_amount_min": {
                "type": "number",
                "description": "Match when mean turnover (amount, in currency) over avg_amount_lookback bars >= this.",
            },
            "min_float_mv": {
                "type": "number",
                "description": "Match when float market cap (流通市值, currency; 100亿 = 1e10) >= this. Pulls the fundamentals axis.",
            },
            "max_float_mv": {
                "type": "number",
                "description": "Match when float market cap <= this.",
            },
            "exclude_suspended": {
                "type": "boolean",
                "description": "Drop symbols halted (停牌) as of asof. Pulls the event axis (akshare suspension snapshot).",
            },
            "limit_up_approx": {
                "type": "boolean",
                "description": (
                    "Match when the asof bar is an approximate limit-up day: "
                    "close within tolerance of the board limit price "
                    "(10%/20%/30% by code prefix) and close equals high."
                ),
            },
            "limit_down_approx": {
                "type": "boolean",
                "description": (
                    "Match when the asof bar is an approximate limit-down day: "
                    "close within tolerance of the board limit-down price "
                    "(10%/20%/30% by code prefix) and close equals low."
                ),
            },
            "scorer_file": {
                "type": "string",
                "description": (
                    "Code-screen mode: path to a single-file Strategy SDK scorer "
                    "(class Strategy). Compiled + smoke-tested, then evaluated per "
                    "symbol; a BUY signal = match. Mutually exclusive with boolean "
                    "conditions and by_strategy."
                ),
            },
            "by_strategy": {
                "type": "string",
                "description": "Code-screen mode: a persisted strategy definition id (sd-…) to evaluate over the universe.",
            },
            "signal_direction": {
                "type": "string",
                "enum": ["buy", "sell", "hold", "any"],
                "description": "Code-screen: which signal direction counts as a match (default buy).",
            },
            "rank_by_diagnostic": {
                "type": "string",
                "description": "Code-screen: order matches by this Signal.diagnostics key (desc). Missing → unranked, sorts last.",
            },
            "rank_by": {
                "type": "string",
                "enum": list(_RANK_METRICS),
                "description": (
                    "Compute this metric for every matched symbol (even when "
                    "not a filter) and order the result by it. Strongest-first "
                    "by default; pair with top_k to keep the top N."
                ),
            },
            "rank_order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Ranking direction when rank_by is set (default desc = strongest first).",
            },
            "top_k": {"type": "integer", "minimum": 1},
            "sort_by": {"type": "string"},
            "sort_desc": {"type": "boolean", "default": False},
            "output_path": {
                "type": "string",
                "description": (
                    "Explicit CSV output path. Defaults to "
                    "~/.doyoutrade/assistant/artifacts/screener_<asof>_<ts>.csv."
                ),
            },
        },
        "required": ["universe"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        data_provider_factory=None,
        fundamentals_provider_factory=None,
        event_provider_factory=None,
        strategy_definition_repository=None,
        strategy_storage=None,
        compiler=None,
        market_bars_repository=None,
    ) -> None:
        """``data_provider_factory`` is for tests — call signature ``(data_source, symbols) -> provider``.

        When ``None`` we build via ``doyoutrade.data.factory.build_trading_data_stack``
        the same way ``data_run`` does. ``fundamentals_provider_factory`` and
        ``event_provider_factory`` are the analogous hooks for the market-cap
        and event axes: ``(data_source) -> provider``.

        ``market_bars_repository`` (the local ``market_bars``
        warehouse) is optional: when wired by the API server, ``_build_data_provider``
        wraps the raw provider with :class:`_LocalFirstScreenProvider` so a
        full-market scan reads already-synced bars locally instead of issuing one
        network round-trip per symbol. When ``None`` (tests / minimal setups) the
        screen behaves exactly as before — direct network fetch per symbol.
        """

        self._data_provider_factory = data_provider_factory
        self._fundamentals_provider_factory = fundamentals_provider_factory
        self._event_provider_factory = event_provider_factory
        self._market_bars_repository = market_bars_repository
        # For --by-strategy: load a persisted definition's source. When these
        # are not injected, --by-strategy degrades to ``by_strategy_unavailable``.
        self._strategy_definition_repository = strategy_definition_repository
        self._strategy_storage = strategy_storage
        self._compiler = compiler

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

        # 2. Validate structural inputs.
        try:
            universe = _validate_universe(kwargs.get("universe"))
            asof = _parse_asof(kwargs.get("asof"))
            conditions = _compile_conditions(kwargs)
        except _InvalidArgument as exc:
            await emit_debug_event(
                "stock_screen.rejected",
                {
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        # Code-screen mode: evaluate a compiled Strategy over the universe.
        # Mutually exclusive with the boolean predicate conditions (the
        # strategy *is* the predicate).
        scorer_file = kwargs.get("scorer_file")
        by_strategy = kwargs.get("by_strategy")
        if scorer_file or by_strategy:
            if scorer_file and by_strategy:
                return await self._reject(
                    "conflicting_screen_mode",
                    "pass exactly one of scorer_file / by_strategy",
                )
            if conditions.has_any():
                return await self._reject(
                    "conflicting_screen_mode",
                    "code-screen (scorer_file / by_strategy) cannot be combined with boolean conditions",
                    "run the predicate conditions and the code scorer as separate screens",
                )
            return await self._run_code_screen(
                universe=universe, asof=asof, kwargs=kwargs,
                scorer_file=scorer_file, by_strategy=by_strategy,
            )

        if not conditions.has_any():
            await emit_debug_event(
                "stock_screen.rejected",
                {
                    "error_code": "no_conditions_specified",
                    "message": "screener invoked with no active conditions",
                    "hint": "pass at least one condition flag (e.g. --rsi-max 30 or --patterns hammer)",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "no_conditions_specified",
                    "no active conditions; pass at least one condition flag",
                    "see `doyoutrade-cli stock screen --help` for the full list",
                ),
                is_error=True,
            )

        interval = str(kwargs.get("interval") or "1d")
        data_source = str(kwargs.get("data_source") or "auto")
        top_k = _coerce_optional_int(kwargs.get("top_k"), "top_k", minimum=1)
        sort_by = kwargs.get("sort_by") or None
        sort_desc = bool(kwargs.get("sort_desc") or False)
        # When rank_by is set and the caller did not pin an explicit sort
        # column, order by the rank metric (strongest-first unless rank_order
        # = asc). An explicit --sort-by always wins.
        if conditions.rank_by is not None and sort_by is None:
            sort_by = _rank_column_name(conditions)
            sort_desc = conditions.rank_desc

        output_path_raw = kwargs.get("output_path")
        output_path = Path(output_path_raw).expanduser() if output_path_raw else _default_result_path(asof)

        lookback_days = _compute_lookback_days(conditions)
        start_dt = asof - timedelta(days=lookback_days)

        await emit_debug_event(
            "stock_screen.validated",
            {
                "universe_size": len(universe),
                "asof": asof.isoformat(),
                "interval": interval,
                "data_source": data_source,
                "lookback_days": lookback_days,
                "max_condition_window": conditions.max_window(),
                "top_k": top_k,
                "sort_by": sort_by,
                "rank_by": conditions.rank_by,
            },
        )

        # 2b. Fetch fundamentals once (batch) when a market-cap condition is
        # active. A failure here is a distinct, top-level error_code — the
        # condition cannot be evaluated for anyone, so we don't pretend.
        fundamentals: dict[str, Any] = {}
        if conditions.needs_fundamentals():
            try:
                fundamentals = await self._fetch_fundamentals(data_source, universe)
            except Exception as exc:
                logger.exception(
                    "stock_screen failed to fetch fundamentals size=%s data_source=%s",
                    len(universe), data_source,
                )
                await emit_debug_event(
                    "stock_screen.failed",
                    {
                        "error_code": "fundamentals_fetch_failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "data_source": data_source,
                        "universe_size": len(universe),
                    },
                )
                return ToolResult(
                    text=format_error_text(
                        "fundamentals_fetch_failed",
                        f"failed to fetch fundamentals via data_source={data_source!r}: {exc}",
                        "check the source (akshare network / qmt base_url); try --data-source explicitly",
                    ),
                    is_error=True,
                )
            await emit_debug_event(
                "stock_screen.fundamentals_loaded",
                {
                    "universe_size": len(universe),
                    "fundamentals_matched": len(fundamentals),
                    "data_source": data_source,
                },
            )

        # 2c. Fetch events (suspension) once when an event condition is active.
        events: dict[str, Any] = {}
        if conditions.needs_events():
            try:
                events = await self._fetch_events(data_source, universe, asof)
            except Exception as exc:
                logger.exception(
                    "stock_screen failed to fetch events size=%s data_source=%s",
                    len(universe), data_source,
                )
                await emit_debug_event(
                    "stock_screen.failed",
                    {
                        "error_code": "events_fetch_failed",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "data_source": data_source,
                        "universe_size": len(universe),
                    },
                )
                return ToolResult(
                    text=format_error_text(
                        "events_fetch_failed",
                        f"failed to fetch events via data_source={data_source!r}: {exc}",
                        "check the source (akshare network); --exclude-suspended needs the suspension snapshot",
                    ),
                    is_error=True,
                )
            await emit_debug_event(
                "stock_screen.events_loaded",
                {
                    "universe_size": len(universe),
                    "events_symbols": len(events),
                    "data_source": data_source,
                },
            )

        # 3. Fetch bars + evaluate per symbol.
        try:
            symbol_results, skipped = await self._scan_universe(
                universe=universe,
                asof=asof,
                start_dt=start_dt,
                interval=interval,
                data_source=data_source,
                conditions=conditions,
                fundamentals=fundamentals,
                events=events,
            )
        except Exception as exc:
            logger.exception(
                "stock_screen failed to scan universe size=%s data_source=%s: %s",
                len(universe), data_source, exc,
            )
            await emit_debug_event(
                "stock_screen.failed",
                {
                    "error_code": "scan_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "data_source": data_source,
                    "universe_size": len(universe),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "scan_failed",
                    f"failed to scan universe via data_source={data_source!r}: {exc}",
                    "check the data_source is configured (data.qmt.base_url / tushare token / akshare network)",
                ),
                is_error=True,
            )

        matched = [r for r in symbol_results if r.matched]

        # 4. Build flat row dicts; sort; truncate.
        rows = self._build_rows(matched)

        # Surface matched symbols that could not be ranked (insufficient
        # history for the rank metric) before truncation hides them.
        if conditions.rank_by is not None:
            rank_col = _rank_column_name(conditions)
            assert rank_col is not None
            unranked = [
                r["symbol"]
                for r in rows
                if r.get(rank_col) is None
                or (isinstance(r.get(rank_col), float) and math.isnan(r[rank_col]))
            ]
            if unranked:
                await emit_debug_event(
                    "screener_rank_skipped",
                    {
                        "rank_by": conditions.rank_by,
                        "rank_column": rank_col,
                        "unranked_count": len(unranked),
                        "unranked_sample": unranked[:20],
                        "hint": (
                            "these matched symbols lacked enough history to "
                            "compute the rank metric; they sort to the end and "
                            "may be dropped by top_k"
                        ),
                    },
                )
                logger.info(
                    "stock_screen rank metric=%s unranked=%d of matched=%d",
                    conditions.rank_by, len(unranked), len(rows),
                )

        rows = _sort_results(rows, sort_by=sort_by, sort_desc=sort_desc)
        if top_k is not None:
            rows = rows[:top_k]

        # 5. Stable column ordering for CSV + preview.
        columns = self._build_column_order(rows)

        result_path = _write_result_csv(rows, columns=columns, output_path=output_path)

        preview = rows[:10]

        payload = {
            "status": "ok",
            "asof": asof.isoformat(),
            "interval": interval,
            "data_source": data_source,
            "universe_size": len(universe),
            "matched": len(rows),
            "skipped": skipped,
            "lookback_days": lookback_days,
            "result_path": str(result_path),
            "columns": columns,
            "preview": preview,
        }
        header = (
            f"Screened {len(universe)} symbols asof {asof.isoformat()} via "
            f"data_source={data_source}: matched={len(rows)} skipped={skipped}; "
            f"CSV at {result_path}."
        )
        return ToolResult(text=append_json_payload(header, payload))

    # ------------------------------------------------------------------

    async def _scan_universe(
        self,
        *,
        universe: list[str],
        asof: date,
        start_dt: date,
        interval: str,
        data_source: str,
        conditions: _CompiledConditions,
        fundamentals: dict[str, Any] | None = None,
        events: dict[str, Any] | None = None,
    ) -> tuple[list[_SymbolResult], int]:
        """Build a data provider, fetch bars, evaluate each symbol. Returns
        ``(results, skipped_count)``. Provider is closed at the end."""

        fundamentals = fundamentals or {}
        events = events or {}

        provider = await self._build_data_provider(data_source, universe)
        skipped = 0
        results: list[_SymbolResult] = []
        try:
            import asyncio

            sem = asyncio.Semaphore(_MAX_PARALLEL_SYMBOLS)
            start_iso = start_dt.isoformat()
            end_iso = asof.isoformat()

            async def _process(symbol: str) -> _SymbolResult | None:
                async with sem:
                    try:
                        bars = list(
                            await provider.get_bars(
                                symbol, start_iso, end_iso, interval=interval
                            )
                        )
                    except Exception as exc:
                        await self._emit_skip(
                            symbol=symbol,
                            reason="bar_fetch_failed",
                            hint=(
                                "the data provider raised while fetching bars; "
                                "check provider availability or use --data-source"
                            ),
                            extra={"error_type": type(exc).__name__, "error": str(exc)},
                        )
                        return None
                    df = _bars_to_dataframe(bars, asof)
                    if df.empty:
                        await self._emit_skip(
                            symbol=symbol,
                            reason="no_bars_before_asof",
                            hint=f"no bars at or before {asof.isoformat()} in [{start_iso}, {end_iso}]",
                        )
                        return None
                    try:
                        evaluated = _evaluate_symbol(symbol, df, conditions, fundamentals, events)
                    except _FundamentalsUnavailable:
                        await self._emit_skip(
                            symbol=symbol,
                            reason="fundamentals_unavailable",
                            hint=(
                                "a market-cap condition is active but the "
                                "fundamentals source has no float_mv for this "
                                "symbol; check the symbol or use --data-source"
                            ),
                        )
                        return None
                    except Exception as exc:
                        await self._emit_skip(
                            symbol=symbol,
                            reason="evaluation_raised",
                            hint="indicator computation raised; report as a bug if reproducible",
                            extra={"error_type": type(exc).__name__, "error": str(exc)},
                        )
                        return None
                    if evaluated is None:
                        await self._emit_skip(
                            symbol=symbol,
                            reason="insufficient_history",
                            hint=(
                                "increase the universe's bar coverage or widen "
                                "the asof window; lookback was auto-sized to "
                                f"{conditions.max_window()} trading bars"
                            ),
                        )
                        return None
                    return evaluated

            tasks = [_process(sym) for sym in universe]
            for fut in asyncio.as_completed(tasks):
                outcome = await fut
                if outcome is None:
                    skipped += 1
                else:
                    results.append(outcome)
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:
                    logger.warning(
                        "stock_screen: provider.aclose() raised data_source=%s err=%s",
                        data_source, exc,
                    )
        return results, skipped

    async def _build_data_provider(self, data_source: str, symbols: list[str]):
        if self._data_provider_factory is not None:
            factory = self._data_provider_factory
            built = factory(data_source, symbols)
            if hasattr(built, "__await__"):
                return await built
            return built
        from doyoutrade.config import get_config
        from doyoutrade.data.account_resolution import resolve_default_market_account
        from doyoutrade.data.factory import build_trading_data_stack

        data_cfg = get_config().data
        account = await resolve_default_market_account()
        provider, _universe, _account = build_trading_data_stack(
            data_source, data_cfg, list(symbols), account=account
        )
        del _universe, _account
        if self._market_bars_repository is not None:
            provider = await self._wrap_local_first(provider, data_cfg, account)
        return provider

    async def _wrap_local_first(self, raw: Any, data_cfg: Any, account: Any) -> Any:
        """Wrap ``raw`` so a screen reads the local ``market_bars`` warehouse first.

        The warehouse rows are keyed by the provider/adjust ``MarketDataSyncService``
        wrote them under — ``market_data.default_provider`` and that provider's
        ``capabilities.default_adjust`` (see ``bootstrap._build_market_data_runtime``).
        We resolve the adjust by constructing the default-provider stack once (no IO —
        construction is lazy) and closing it. Any failure here is non-fatal: we log,
        emit ``stock_screen.cache.disabled``, and return the raw provider so the screen
        still works over the network (zero regression)."""

        from doyoutrade.config import get_config
        from doyoutrade.data.factory import build_trading_data_stack

        try:
            warehouse_provider = get_config().market_data.default_provider
            probe, _u, _a = build_trading_data_stack(
                warehouse_provider, data_cfg, [], account=account
            )
            del _u, _a
        except Exception as exc:  # noqa: BLE001 — non-fatal: degrade to network
            logger.warning(
                "stock_screen: could not resolve local-cache warehouse key "
                "(error_type=%s error=%s); screening without the local warehouse",
                type(exc).__name__, exc,
            )
            await emit_debug_event(
                "stock_screen.cache.disabled",
                {
                    "reason": "warehouse_key_resolution_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": "screen falls back to direct network fetch; check market_data.default_provider",
                },
            )
            return raw
        try:
            warehouse_adjust = probe.capabilities.default_adjust
        finally:
            close = getattr(probe, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 — cleanup; surfaced as warning
                    logger.warning(
                        "stock_screen: warehouse-key probe aclose raised error_type=%s error=%s",
                        type(exc).__name__, exc,
                    )
        return _LocalFirstScreenProvider(
            repository=self._market_bars_repository,
            upstream=raw,
            provider=warehouse_provider,
            adjust=warehouse_adjust,
        )

    async def _fetch_fundamentals(self, data_source: str, symbols: list[str]) -> dict[str, Any]:
        """Batch-fetch fundamentals for the universe (one snapshot when akshare)."""
        if self._fundamentals_provider_factory is not None:
            provider = self._fundamentals_provider_factory(data_source)
        else:
            from doyoutrade.config import get_config
            from doyoutrade.data.account_resolution import resolve_default_market_account
            from doyoutrade.data.factory import build_fundamentals_provider

            account = await resolve_default_market_account()
            provider = build_fundamentals_provider(data_source, get_config().data, account)
        try:
            return await provider.get_fundamentals_batch(list(symbols))
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stock_screen: fundamentals provider.aclose() raised: %s", exc)

    async def _fetch_events(self, data_source: str, symbols: list[str], asof: date) -> dict[str, Any]:
        """Batch-fetch events (suspension snapshot) for the universe."""
        if self._event_provider_factory is not None:
            provider = self._event_provider_factory(data_source)
        else:
            from doyoutrade.config import get_config
            from doyoutrade.data.factory import build_event_provider

            provider = build_event_provider(data_source, get_config().data)
        try:
            return await provider.get_events_batch(list(symbols), asof=asof.isoformat())
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stock_screen: event provider.aclose() raised: %s", exc)

    async def _reject(self, error_code: str, message: str, hint: str | None = None) -> ToolResult:
        await emit_debug_event(
            "stock_screen.rejected",
            {"error_code": error_code, "message": message, "hint": hint},
        )
        return ToolResult(text=format_error_text(error_code, message, hint), is_error=True)

    async def _run_code_screen(
        self,
        *,
        universe: list[str],
        asof: date,
        kwargs: dict[str, Any],
        scorer_file: Any,
        by_strategy: Any,
    ) -> ToolResult:
        """Compile a Strategy SDK scorer and evaluate it over the universe.

        Reuses the validated-code path (StrategyCompiler.validate_directory +
        smoke_test) and StrategyRunner.evaluate_signals_for_screen — pure
        compute, NO run_cycle / cycle_runs. A symbol's match is decided by its
        Signal direction; ``rank_by_diagnostic`` orders matches by a
        Signal.diagnostics key.
        """

        import tempfile
        from datetime import datetime, timezone
        from decimal import Decimal

        data_source = str(kwargs.get("data_source") or "auto")
        top_k = _coerce_optional_int(kwargs.get("top_k"), "top_k", minimum=1)
        signal_direction = str(kwargs.get("signal_direction") or "buy").strip().lower()
        if signal_direction not in ("buy", "sell", "hold", "any"):
            return await self._reject(
                "invalid_condition_value",
                f"signal_direction={signal_direction!r} not in buy/sell/hold/any",
            )
        rank_diag = kwargs.get("rank_by_diagnostic") or None
        output_path_raw = kwargs.get("output_path")
        output_path = (
            Path(output_path_raw).expanduser() if output_path_raw else _default_result_path(asof)
        )

        compiler = self._compiler
        if compiler is None:
            from doyoutrade.strategy_runtime.compiler import StrategyCompiler

            compiler = StrategyCompiler()

        tmpdir_ctx: Any = None
        try:
            # 1. Resolve (code_root, class_name).
            if scorer_file:
                src_path = Path(str(scorer_file)).expanduser()
                if not src_path.is_file():
                    return await self._reject("scorer_file_not_found", f"scorer_file not found: {src_path}")
                tmpdir_ctx = tempfile.TemporaryDirectory(prefix="doyoutrade_screen_scorer_")
                code_root = Path(tmpdir_ctx.name)
                (code_root / "strategy.py").write_text(
                    src_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
                class_name = "Strategy"
                scorer_id = src_path.name
            else:
                if self._strategy_definition_repository is None:
                    return await self._reject(
                        "by_strategy_unavailable",
                        "by_strategy needs the strategy definition repository, not wired in this context",
                        "use scorer_file, or run via the API server where the repository is available",
                    )
                sd_id = str(by_strategy)
                try:
                    snap = await self._strategy_definition_repository.get_definition(sd_id)
                    _version, code_root = await self._strategy_definition_repository.read_current_code(sd_id)
                except Exception as exc:  # noqa: BLE001
                    return await self._reject("strategy_not_found", f"could not load strategy {sd_id!r}: {exc}")
                class_name = getattr(snap, "class_name", None) or "Strategy"
                scorer_id = sd_id

            # 2. Compile + smoke (same gate as `sdk validate`).
            compile_result = compiler.validate_directory(code_root, strategy_class_name=class_name)
            if not compile_result.success or compile_result.artifact is None:
                return await self._reject(
                    compile_result.error_code or "compile_failed",
                    "; ".join(compile_result.errors) or "strategy failed to compile",
                    "fix the scorer (see strategy-definition-authoring); error_code mirrors sdk validate",
                )
            artifact = compile_result.artifact
            smoke = compiler.smoke_test(artifact)
            if not smoke.success:
                return await self._reject(
                    smoke.error_code or "smoke_failed",
                    smoke.error_message or "strategy crashed during smoke test",
                    "the scorer raised on synthetic data; fix before screening",
                )
        finally:
            if tmpdir_ctx is not None:
                tmpdir_ctx.cleanup()

        # 3. Build a data provider + runner and evaluate per symbol.
        from doyoutrade.execution.position_manager import PositionManager
        from doyoutrade.strategy_sdk.context import AccountView
        from doyoutrade.strategy_sdk.history_fetcher import BarsHistoryFetcher
        from doyoutrade.strategy_sdk.runner import StrategyRunner

        await emit_debug_event(
            "stock_screen.code_screen_started",
            {
                "scorer": scorer_id, "class_name": class_name,
                "universe_size": len(universe), "data_source": data_source,
                "signal_direction": signal_direction,
            },
        )
        provider = await self._build_data_provider(data_source, universe)
        try:
            runner = StrategyRunner(
                strategy=artifact.strategy_class(),
                position_manager=PositionManager(),
                history_fetcher=BarsHistoryFetcher(data_provider=provider),
                parameters={},
            )
            as_of_dt = datetime(asof.year, asof.month, asof.day, tzinfo=timezone.utc)
            account_view = AccountView(cash=Decimal("100000000"), equity=Decimal("100000000"))
            signals = await runner.evaluate_signals_for_screen(
                list(universe), as_of=as_of_dt, account_view=account_view, is_backtest=True
            )
        except Exception as exc:
            logger.exception("stock_screen code-screen evaluation failed scorer=%s", scorer_id)
            await emit_debug_event(
                "stock_screen.failed",
                {"error_code": "code_screen_failed", "error_type": type(exc).__name__, "message": str(exc)},
            )
            return ToolResult(
                text=format_error_text(
                    "code_screen_failed",
                    f"strategy evaluation failed: {exc}",
                    "check the data_source and the scorer's data needs",
                ),
                is_error=True,
            )
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stock_screen: code-screen provider.aclose() raised: %s", exc)

        # 4. Filter by signal direction; collect rows; rank by diagnostic.
        rows: list[dict[str, Any]] = []
        skipped = 0
        for sym in universe:
            sig = signals.get(sym)
            if sig is None:
                skipped += 1
                continue
            direction = getattr(sig.direction, "value", str(sig.direction))
            if signal_direction != "any" and direction != signal_direction:
                continue
            row: dict[str, Any] = {
                "symbol": sym,
                "direction": direction,
                "tag": sig.tag,
                "rationale": sig.rationale,
            }
            for k, v in dict(sig.diagnostics or {}).items():
                row[f"diag.{k}"] = v
            rows.append(row)

        if rank_diag:
            rows = _sort_results(rows, sort_by=f"diag.{rank_diag}", sort_desc=True)
        if top_k is not None:
            rows = rows[:top_k]

        # Stable column order: identifiers first, diagnostics after.
        preferred = ["symbol", "direction", "tag", "rationale"]
        extras = sorted({k for r in rows for k in r if k not in preferred})
        columns = [c for c in preferred if any(c in r for r in rows)] + extras
        result_path = _write_result_csv(rows, columns=columns, output_path=output_path)

        payload = {
            "status": "ok",
            "mode": "code",
            "asof": asof.isoformat(),
            "data_source": data_source,
            "scorer": scorer_id,
            "signal_direction": signal_direction,
            "universe_size": len(universe),
            "matched": len(rows),
            "skipped": skipped,
            "result_path": str(result_path),
            "columns": columns,
            "preview": rows[:10],
        }
        await emit_debug_event(
            "stock_screen.created",
            {"tool": self.name, "mode": "code", "scorer": scorer_id,
             "matched": len(rows), "skipped": skipped, "result_path": str(result_path)},
        )
        header = (
            f"Code-screened {len(universe)} symbols via {scorer_id}: "
            f"matched={len(rows)} skipped={skipped}; CSV at {result_path}."
        )
        return ToolResult(text=append_json_payload(header, payload))

    async def _emit_skip(
        self,
        *,
        symbol: str,
        reason: str,
        hint: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"symbol": symbol, "reason": reason, "hint": hint}
        if extra:
            payload.update(extra)
        await emit_debug_event("screener_symbol_skipped", payload)
        logger.info(
            "stock_screen skipped symbol=%s reason=%s%s",
            symbol,
            reason,
            f" detail={extra}" if extra else "",
        )

    # ------------------------------------------------------------------

    def _build_rows(self, matched: list[_SymbolResult]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for res in matched:
            row: dict[str, Any] = {
                "symbol": res.symbol,
                "matched_conditions": ";".join(res.matched_conditions),
            }
            row.update(res.columns)
            rows.append(row)
        return rows

    def _build_column_order(self, rows: list[dict[str, Any]]) -> list[str]:
        # Stable header: identifiers first, then frequently-used computed
        # columns, then any extras in alphabetical order.
        preferred = [
            "symbol",
            "matched_conditions",
            "close",
            "rsi",
            "pct_change",
            "volume_ratio",
            "avg_amount",
            "float_mv",
            "bar_count",
        ]
        seen: set[str] = set()
        ordered: list[str] = []
        for col in preferred:
            if any(col in row for row in rows):
                ordered.append(col)
                seen.add(col)
        extras: set[str] = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    extras.add(key)
        ordered.extend(sorted(extras))
        return ordered


__all__ = [
    "StockScreenTool",
    "_PATTERN_NAMES",
    "_MACD_MODES",
    "_BOLLINGER_MODES",
    "_KDJ_MODES",
    "_KELTNER_MODES",
    "_DONCHIAN_MODES",
]
