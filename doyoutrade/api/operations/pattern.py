from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path
from typing import Any

# Import the boolean primitives directly (not the module) so Pyright can
# resolve them despite the self-import in doyoutrade/strategy_sdk/__init__.py.
# These are the single source of truth shared with strategy code; keeping
# the analysis tool delegating prevents the hammer/engulfing/doji
# definitions from drifting between SDK and analysis surfaces.
from doyoutrade.strategy_sdk.patterns import (
    is_bearish_engulfing as _is_bearish_engulfing,
    is_bullish_engulfing as _is_bullish_engulfing,
    is_doji as _is_doji,
    is_hammer as _is_hammer,
)
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text


def _get_artifacts_root() -> Path:
    return Path.home() / ".doyoutrade" / "assistant" / "artifacts"


def _safe_code(code: str) -> str:
    """Sanitize code string for use in filenames."""
    return code.replace("/", "_").replace("\\", "_").replace(":", "_")


# ---------------------------------------------------------------------------
# Pattern detection functions (copied from Vibe-Trading pattern_tool.py)
# ---------------------------------------------------------------------------


def find_peaks_valleys(close: pd.Series, window: int = 5) -> dict:
    """Detect peaks and valleys in a price series.

    Args:
        close: Closing price series.
        window: Half-window size; effective window is 2*window+1.

    Returns:
        Dict with keys "peaks" and "valleys", each a list of integer indices.
    """
    n = len(close)
    if n < 2 * window + 1:
        return {"peaks": [], "valleys": []}

    values = close.values.astype(float)
    peaks, valleys = [], []

    for i in range(window, n - window):
        seg = values[i - window : i + window + 1]
        if np.isnan(values[i]):
            continue
        seg = seg[~np.isnan(seg)]
        if len(seg) == 0:
            continue
        if values[i] == np.max(seg):
            peaks.append(i)
        if values[i] == np.min(seg):
            valleys.append(i)

    return {"peaks": peaks, "valleys": valleys}


def candlestick_patterns(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Detect candlestick patterns: hammer (bullish), and engulfing (bullish/bearish).

    Delegates to :mod:`doyoutrade.strategy_sdk.patterns` so the analysis tool
    and the strategy SDK share a single, vetted definition of each primitive
    (hammer / bullish-engulfing / bearish-engulfing). Engulfing flags overwrite
    hammer flags at the same bar (matching prior behaviour); the per-bar doji
    count is reported separately by :func:`_candlestick_summary`.

    Args:
        open_: Open price series.
        high: High price series.
        low: Low price series.
        close: Close price series.

    Returns:
        Series with values -1 (bearish), 0 (neutral), 1 (bullish).
    """
    result = pd.Series(0, index=close.index, dtype=int)

    is_hammer = _is_hammer(open_, high, low, close)
    result = result.where(~is_hammer, 1)

    engulf_bull = _is_bullish_engulfing(open_, high, low, close)
    result = result.where(~engulf_bull, 1)

    engulf_bear = _is_bearish_engulfing(open_, high, low, close)
    result = result.where(~engulf_bear, -1)

    return result


def support_resistance(
    close: pd.Series, window: int = 20, num_levels: int = 3
) -> dict:
    """Compute support and resistance levels via peak/valley clustering.

    Args:
        close: Closing price series.
        window: Peak/valley detection window.
        num_levels: Maximum number of levels to return.

    Returns:
        Dict with keys "support" and "resistance", each a list of price levels.
    """
    pv = find_peaks_valleys(close, window=window)
    values = close.values.astype(float)

    peak_prices = [
        float(values[i]) for i in pv["peaks"] if not np.isnan(values[i])
    ]
    valley_prices = [
        float(values[i]) for i in pv["valleys"] if not np.isnan(values[i])
    ]

    def cluster(prices: list, n: int) -> list:
        if not prices:
            return []
        sp = sorted(prices)
        if len(sp) <= n:
            return sp
        clusters: list[list[float]] = [[sp[0]]]
        rng = sp[-1] - sp[0]
        thr = rng * 0.05 if rng > 0 else 1.0
        for p in sp[1:]:
            if abs(p - np.mean(clusters[-1])) <= thr:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        centers = [(len(c), float(np.mean(c))) for c in clusters]
        centers.sort(reverse=True)
        return [c for _, c in centers[:n]]

    return {
        "support": cluster(valley_prices, num_levels),
        "resistance": cluster(peak_prices, num_levels),
    }


def trend_line_slope(close: pd.Series, window: int = 20) -> pd.Series:
    """Compute rolling linear-fit slope.

    Args:
        close: Closing price series.
        window: Fitting window size.

    Returns:
        Series of slope values; first window-1 entries are NaN.
    """
    n = len(close)
    slopes = np.full(n, np.nan)
    values = close.values.astype(float)
    x = np.arange(window, dtype=float)

    for i in range(window - 1, n):
        seg = values[i - window + 1 : i + 1]
        if np.any(np.isnan(seg)):
            continue
        slopes[i] = np.polyfit(x, seg, 1)[0]

    return pd.Series(slopes, index=close.index)


def head_and_shoulders(close: pd.Series, window: int = 10) -> pd.Series:
    """Detect head-and-shoulders top pattern.

    Args:
        close: Closing price series.
        window: Peak/valley detection window.

    Returns:
        Series with 1 where pattern is detected, 0 otherwise.
    """
    result = pd.Series(0, index=close.index, dtype=int)
    pv = find_peaks_valleys(close, window=window)
    peaks = pv["peaks"]
    values = close.values.astype(float)

    if len(peaks) < 3:
        return result

    for i in range(len(peaks) - 2):
        lv, hv, rv = values[peaks[i]], values[peaks[i + 1]], values[peaks[i + 2]]
        if any(np.isnan(x) for x in (lv, hv, rv)):
            continue
        if hv <= lv or hv <= rv:
            continue
        avg = (lv + rv) / 2
        if avg == 0 or abs(lv - rv) / avg > 0.05:
            continue
        result.iloc[peaks[i + 1]] = 1

    return result


def double_top_bottom(close: pd.Series, window: int = 10) -> pd.Series:
    """Detect double-top and double-bottom patterns.

    Args:
        close: Closing price series.
        window: Peak/valley detection window.

    Returns:
        Series with 1 (double top), -1 (double bottom), or 0 (none).
    """
    result = pd.Series(0, index=close.index, dtype=int)
    pv = find_peaks_valleys(close, window=window)
    values = close.values.astype(float)

    for i in range(len(pv["peaks"]) - 1):
        v1, v2 = values[pv["peaks"][i]], values[pv["peaks"][i + 1]]
        if np.isnan(v1) or np.isnan(v2):
            continue
        avg = (v1 + v2) / 2
        if avg != 0 and abs(v1 - v2) / avg < 0.03:
            result.iloc[pv["peaks"][i + 1]] = 1

    for i in range(len(pv["valleys"]) - 1):
        v1, v2 = values[pv["valleys"][i]], values[pv["valleys"][i + 1]]
        if np.isnan(v1) or np.isnan(v2):
            continue
        avg = (v1 + v2) / 2
        if avg != 0 and abs(v1 - v2) / abs(avg) < 0.03:
            if result.iloc[pv["valleys"][i + 1]] == 0:
                result.iloc[pv["valleys"][i + 1]] = -1

    return result


def triangle(close: pd.Series, window: int = 20) -> pd.Series:
    """Detect triangle patterns.

    Args:
        close: Closing price series.
        window: Detection window size.

    Returns:
        Series with 1 (ascending triangle), -1 (descending triangle), or 0 (none).
    """
    n = len(close)
    result = pd.Series(0, index=close.index, dtype=int)
    values = close.values.astype(float)

    for i in range(window, n):
        seg = pd.Series(values[i - window : i + 1])
        pv = find_peaks_valleys(seg, window=max(2, window // 5))
        if len(pv["peaks"]) < 2 or len(pv["valleys"]) < 2:
            continue
        pvals = [float(seg.iloc[p]) for p in pv["peaks"]]
        vvals = [float(seg.iloc[v]) for v in pv["valleys"]]
        ps = (
            np.polyfit(np.arange(len(pvals), dtype=float), pvals, 1)[0]
            if len(pvals) >= 2
            else 0.0
        )
        vs = (
            np.polyfit(np.arange(len(vvals), dtype=float), vvals, 1)[0]
            if len(vvals) >= 2
            else 0.0
        )
        rng = max(pvals) - min(vvals)
        if rng == 0:
            continue
        flat = rng * 0.02
        if vs > flat and abs(ps) < flat:
            result.iloc[i] = 1
        elif ps < -flat and abs(vs) < flat:
            result.iloc[i] = -1

    return result


def broadening(close: pd.Series, window: int = 20) -> pd.Series:
    """Detect broadening (megaphone) patterns.

    Args:
        close: Closing price series.
        window: Detection window size.

    Returns:
        Series with 1 where broadening pattern is detected, 0 otherwise.
    """
    n = len(close)
    result = pd.Series(0, index=close.index, dtype=int)
    values = close.values.astype(float)

    for i in range(window, n):
        seg = pd.Series(values[i - window : i + 1])
        pv = find_peaks_valleys(seg, window=max(2, window // 5))
        if len(pv["peaks"]) < 2 or len(pv["valleys"]) < 2:
            continue
        pvals = [float(seg.iloc[p]) for p in pv["peaks"]]
        vvals = [float(seg.iloc[v]) for v in pv["valleys"]]
        peaks_rising = all(pvals[j + 1] > pvals[j] for j in range(len(pvals) - 1))
        valleys_falling = all(vvals[j + 1] < vvals[j] for j in range(len(vvals) - 1))
        if peaks_rising and valleys_falling:
            result.iloc[i] = 1

    return result


# ---------------------------------------------------------------------------
# Pattern registry and aggregator
# ---------------------------------------------------------------------------

def _candlestick_summary(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    """Return candlestick pattern counts grouped by direction.

    The underlying candlestick_patterns() function returns a single value per bar
    (1=bullish, -1=bearish, 0=neutral), where engulfing patterns overwrite hammer
    flags at the same index. This function reports what can actually be distinguished
    without modifying the lower-level function: Bullish/Bearish/Neutral counts plus
    a separate Doji count (delegated to ``doyoutrade.strategy_sdk.patterns.is_doji``
    so the analysis tool and the strategy SDK share one definition).
    """
    result = candlestick_patterns(open_, high, low, close)
    is_doji = _is_doji(open_, high, low, close)

    return {
        "Bullish": int((result == 1).sum()),
        "Bearish": int((result == -1).sum()),
        "Neutral": int((result == 0).sum()),
        "Doji": int(is_doji.sum()),
    }


def _run_patterns_for_df(
    df: pd.DataFrame, window: int, selected_patterns: list[str] | None = None
) -> dict[str, Any]:
    """Run selected pattern detectors on a single OHLCV DataFrame.

    Args:
        df: OHLCV DataFrame.
        window: Detection window size.
        selected_patterns: List of pattern names to run. None means all patterns.
    """
    # All available pattern names
    ALL_PATTERNS = [
        "candlestick",
        "support_resistance",
        "head_and_shoulders",
        "double_top_bottom",
        "triangle",
        "broadening",
        "trend_slope",
    ]
    if selected_patterns is None:
        selected_patterns = ALL_PATTERNS

    close = df["close"]
    open_ = df["open"]
    high = df["high"]
    low = df["low"]

    result: dict[str, Any] = {}

    # Candlestick
    if "candlestick" in selected_patterns:
        result["candlestick"] = _candlestick_summary(open_, high, low, close)

    # Support/Resistance
    if "support_resistance" in selected_patterns:
        result["support_resistance"] = support_resistance(close, window=window)

    # Head and shoulders
    if "head_and_shoulders" in selected_patterns:
        hs_count = int(head_and_shoulders(close, window=window).sum())
        result["head_and_shoulders"] = {"count": hs_count}

    # Double top/bottom
    if "double_top_bottom" in selected_patterns:
        dtb = double_top_bottom(close, window=window)
        result["double_top_bottom"] = {
            "double_top": int((dtb == 1).sum()),
            "double_bottom": int((dtb == -1).sum()),
        }

    # Triangle
    if "triangle" in selected_patterns:
        tri = triangle(close, window=window)
        result["triangle"] = {
            "ascending": int((tri == 1).sum()),
            "descending": int((tri == -1).sum()),
        }

    # Broadening
    if "broadening" in selected_patterns:
        broad_count = int(broadening(close, window=window).sum())
        result["broadening"] = {"count": broad_count}

    # Trend slope
    if "trend_slope" in selected_patterns:
        slopes = trend_line_slope(close, window=window)
        mean_slope = float(slopes.dropna().mean()) if len(df) > window else 0.0
        result["trend_slope"] = {"mean_slope": mean_slope}

    return result


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class PatternRecognitionTool(OperationHandler):
    name = "pattern_recognition"
    description = (
        "Run chart pattern detection on OHLCV CSV data. "
        "Reads from ~/.doyoutrade/assistant/artifacts/ohlcv_{code}.csv. "
        "Supports: candlestick (doji, hammer, engulfing), support_resistance, "
        "head_and_shoulders, double_top_bottom, triangle, broadening, trend_slope."
    )
    category = "analysis"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Symbol code (reads from ohlcv_{code}.csv)",
            },
            "patterns": {
                "type": "string",
                "description": 'Comma-separated pattern names or "all"',
                "default": "all",
            },
            "window": {
                "type": "integer",
                "description": "Detection window size",
                "default": 10,
            },
        },
        "required": ["code"],
    }

    def execute_sync(self, code: str, patterns: str = "all", window: int = 10) -> ToolResult:
        """Execute pattern detection synchronously."""
        ALL_PATTERNS = [
            "candlestick",
            "support_resistance",
            "head_and_shoulders",
            "double_top_bottom",
            "triangle",
            "broadening",
            "trend_slope",
        ]

        if patterns == "all":
            selected_patterns: list[str] | None = None
        else:
            requested = [p.strip() for p in patterns.split(",")]
            invalid = [p for p in requested if p not in ALL_PATTERNS]
            if invalid:
                msg = f"Unknown patterns: {invalid}. Available: {ALL_PATTERNS}"
                return ToolResult(
                    text=format_error_text("unknown_patterns", msg),
                    is_error=True,
                )
            selected_patterns = requested

        root = _get_artifacts_root()
        safe = _safe_code(code)
        csv_path = root / f"ohlcv_{safe}.csv"

        if not csv_path.exists():
            return ToolResult(
                text=format_error_text(
                    "ohlcv_csv_missing",
                    "CSV not found. Run doyoutrade-cli data run first.",
                    "call `doyoutrade-cli data run <code>` to populate the OHLCV cache.",
                ),
                is_error=True,
            )

        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        except Exception as exc:
            return ToolResult(
                text=format_error_text("ohlcv_csv_read_failed", f"Failed to read CSV: {exc}"),
                is_error=True,
            )

        if df.empty:
            return ToolResult(
                text=format_error_text("ohlcv_csv_empty", "CSV is empty."),
                is_error=True,
            )

        patterns_result = _run_patterns_for_df(df, window=window, selected_patterns=selected_patterns)
        bars = int(len(df))
        match_count = sum(
            1 for v in patterns_result.values() if isinstance(v, dict) and v.get("detected")
        ) if isinstance(patterns_result, dict) else 0
        header = (
            f"Pattern scan for {code} over {bars} bars (window={window}): "
            f"{match_count} pattern group(s) matched."
        )
        payload = {
            "status": "ok",
            "code": code,
            "window": window,
            "patterns": patterns_result,
        }
        return ToolResult(text=append_json_payload(header, payload))

    async def execute(self, code: str, patterns: str = "all", window: int = 10) -> ToolResult:
        # Pattern detection is CPU-bound pandas; run in sync for simplicity
        return self.execute_sync(code=code, patterns=patterns, window=window)
