"""Lookahead-safe chart-pattern primitives for ``Strategy`` strategy code.

This module is the **single source of truth** for the structural /
chart-pattern formulas inside authored strategies. The functions here are
designed so that ``populate_indicators`` can pre-compute them on the full
``DataFrame`` and ``on_bar`` can read ``.iloc[-1]`` without ever leaking
future information into the current-bar decision:

- Every output Series is index-aligned with the input.
- Every value at index ``i`` depends **only on bars at indices ``<= i``**,
  using current-bar values and ``.shift(N)`` of past bars. No
  ``.shift(-N)``, no ``rolling(...).center=True`` reads forwarded into
  ``i``, no "set pivot at the bar where it was made" — pivots are stamped
  at the **confirmation bar** ``i + right`` because that is the earliest
  bar at which the pivot can be known.
- Warm-up bars (where there is not yet enough history) produce ``False``
  for boolean outputs and ``NaN`` for float outputs. Strategies must gate
  ``.iloc[-1]`` reads through ``startup_history``.

Why the dedicated module: ``doyoutrade.api.operations.pattern`` exists for
the operator-facing analysis tool and uses ``find_peaks_valleys`` with a
two-sided rolling window that **does** look forward in time
(``values[i - window : i + window + 1]`` reads ``i + window`` future
bars). That formulation is fine for offline pattern reports computed once
on a finished series, but feeding it into a backtest as-is silently leaks
the future into the entry decision. The functions in this module use the
same conventions and thresholds as ``pattern.py`` but only ever stamp
results at causal positions.

Surface (all functions return ``pd.Series``; bool / float as documented):

- **Candlestick** (per-bar; depends on current + previous bar only):
  ``is_doji``, ``is_hammer``, ``is_inverted_hammer``,
  ``is_bullish_engulfing``, ``is_bearish_engulfing``,
  ``is_bullish_harami``, ``is_bearish_harami``.
- **Price levels / breakouts / bounces**: ``prior_high``, ``prior_low``,
  ``broke_above``, ``broke_below``, ``touched_above``, ``touched_below``,
  ``bounced_from``.
- **Causal swings**: ``swing_high``, ``swing_low``,
  ``last_swing_high_level``, ``last_swing_low_level``. Pivot at offset
  ``i`` (i.e. ``high[i]`` is the local max over
  ``high[i-left : i+right+1]``) is marked ``True`` at index ``i + right``
  — the first bar at which the pivot is unambiguously confirmed.
- **Structural patterns** (built on confirmed pivots): ``double_top``,
  ``double_bottom``, ``head_and_shoulders``, ``triangle``, ``broadening``.

Input contract mirrors :mod:`doyoutrade.strategy_sdk.indicators`: pass
``df[col]`` slices (``open``, ``high``, ``low``, ``close``) from the
strategy's ``DataFrame``. Passing a non-Series raises ``TypeError`` with
the actual type + value; bad windows / tolerances raise ``ValueError``.
"""

from __future__ import annotations

from typing import Union, cast

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Input guards (duplicated from indicators.py per CLAUDE.md — patterns.py is
# a self-contained, vetted module and must not depend on private helpers
# from a sibling module).
# ---------------------------------------------------------------------------


def _ensure_series(values: object, name: str) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise TypeError(
            f"{name} must be a pandas.Series, got {type(values).__name__}; "
            f"pass df['{name}'] from data_map[symbol]."
        )
    return values


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"{name} must be an int, got {type(value).__name__}({value!r})"
        )
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _non_negative_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"{name} must be an int, got {type(value).__name__}({value!r})"
        )
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return value


def _non_negative_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} must be a number, got {type(value).__name__}({value!r})"
        )
    v = float(value)
    if not v >= 0.0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return v


LevelLike = Union[pd.Series, int, float]


def _ensure_level(value: object, name: str) -> LevelLike:
    """Validate a ``level`` argument: Series or numeric scalar (not bool)."""
    if isinstance(value, bool):
        raise TypeError(
            f"{name} must be a pandas.Series or number, got bool({value!r})"
        )
    if isinstance(value, (pd.Series, int, float)):
        return value
    raise TypeError(
        f"{name} must be a pandas.Series or number, "
        f"got {type(value).__name__}({value!r})"
    )


def _level_to_series(level: LevelLike, like: pd.Series) -> pd.Series:
    """Broadcast a scalar level into a constant Series aligned with ``like``."""
    if isinstance(level, pd.Series):
        return level
    return pd.Series(float(level), index=like.index, dtype="float64")


# ---------------------------------------------------------------------------
# Candlestick patterns
#
# Each function depends on the current bar and (for harami / engulfing) the
# immediately previous bar via ``.shift(1)``. The first bar of any output is
# ``False`` because ``shift(1)`` yields ``NaN`` on it; ``& / |`` with NaN
# stays NaN and the final ``.fillna(False)`` collapses it to ``False``.
# ---------------------------------------------------------------------------


def _body_and_shadows(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Internal: compute body, safe_range, upper_shadow, lower_shadow.

    Matches ``doyoutrade.api.operations.pattern.candlestick_patterns`` so the
    primitives here agree with the operator-facing report.
    """
    body = (close - open_).abs()
    total_range = high - low
    safe_range = total_range.replace(0, np.nan)
    upper_shadow = high - pd.concat([open_, close], axis=1).max(axis=1)
    lower_shadow = pd.concat([open_, close], axis=1).min(axis=1) - low
    return body, safe_range, upper_shadow, lower_shadow


def is_doji(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    body_frac: float = 0.10,
) -> pd.Series:
    """True where ``body / (high - low) < body_frac`` (default 10%).

    Doji is the classic indecision candle: a real body that is small
    relative to the full bar range.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    frac = _non_negative_float(body_frac, "body_frac")
    body, safe_range, _, _ = _body_and_shadows(open_, high, low, close)
    out = (body / safe_range) < frac
    return cast(pd.Series, out.fillna(False).astype(bool))


def is_hammer(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Hammer: long lower shadow, tiny upper shadow, real body, not a doji.

    Matches ``pattern.py`` line 82: ``lower_shadow > 2 * body`` AND
    ``upper_shadow < body`` AND NOT doji.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body, safe_range, upper_shadow, lower_shadow = _body_and_shadows(
        open_, high, low, close
    )
    doji_mask = (body / safe_range) < 0.10
    cond = (lower_shadow > 2 * body) & (upper_shadow < body) & (~doji_mask.fillna(False))
    return cast(pd.Series, cond.fillna(False).astype(bool))


def is_inverted_hammer(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Inverted hammer: long upper shadow, tiny lower shadow, real body.

    Mirror of :func:`is_hammer`: ``upper_shadow > 2 * body`` AND
    ``lower_shadow < body`` AND NOT doji.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body, safe_range, upper_shadow, lower_shadow = _body_and_shadows(
        open_, high, low, close
    )
    doji_mask = (body / safe_range) < 0.10
    cond = (upper_shadow > 2 * body) & (lower_shadow < body) & (~doji_mask.fillna(False))
    return cast(pd.Series, cond.fillna(False).astype(bool))


def is_bullish_engulfing(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Bullish engulfing: today's bullish body engulfs yesterday's bearish body.

    Matches ``pattern.py`` lines 85-94.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body = (close - open_).abs()
    prev_bearish = close.shift(1) < open_.shift(1)
    curr_bullish = close > open_
    cond = (
        prev_bearish
        & curr_bullish
        & (open_ <= close.shift(1))
        & (close >= open_.shift(1))
        & (body > body.shift(1))
    )
    return cast(pd.Series, cond.fillna(False).astype(bool))


def is_bearish_engulfing(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Bearish engulfing: today's bearish body engulfs yesterday's bullish body.

    Matches ``pattern.py`` lines 96-105.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body = (close - open_).abs()
    prev_bullish = close.shift(1) > open_.shift(1)
    curr_bearish = close < open_
    cond = (
        prev_bullish
        & curr_bearish
        & (open_ >= close.shift(1))
        & (close <= open_.shift(1))
        & (body > body.shift(1))
    )
    return cast(pd.Series, cond.fillna(False).astype(bool))


def is_bullish_harami(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Bullish harami: prior large bearish body fully contains today's small bullish body.

    Definition:
    - Previous bar bearish: ``close.shift(1) < open_.shift(1)``.
    - Previous bar is "large": its body occupies more than half of its range
      (``body.shift(1) / safe_range.shift(1) > 0.5``).
    - Current bar bullish with a smaller body than the prior bar.
    - Current bar's body is contained inside the prior bar's body:
      ``open_ >= close.shift(1)`` AND ``close <= open_.shift(1)``.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body, safe_range, _, _ = _body_and_shadows(open_, high, low, close)
    prev_bearish = close.shift(1) < open_.shift(1)
    prev_large = (body.shift(1) / safe_range.shift(1)) > 0.5
    curr_bullish = close > open_
    body_smaller = body < body.shift(1)
    contained = (open_ >= close.shift(1)) & (close <= open_.shift(1))
    cond = prev_bearish & prev_large & curr_bullish & body_smaller & contained
    return cast(pd.Series, cond.fillna(False).astype(bool))


def is_bearish_harami(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Bearish harami: prior large bullish body fully contains today's small bearish body.

    Mirror of :func:`is_bullish_harami`.
    """
    _ensure_series(open_, "open_")
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    body, safe_range, _, _ = _body_and_shadows(open_, high, low, close)
    prev_bullish = close.shift(1) > open_.shift(1)
    prev_large = (body.shift(1) / safe_range.shift(1)) > 0.5
    curr_bearish = close < open_
    body_smaller = body < body.shift(1)
    contained = (open_ <= close.shift(1)) & (close >= open_.shift(1))
    cond = prev_bullish & prev_large & curr_bearish & body_smaller & contained
    return cast(pd.Series, cond.fillna(False).astype(bool))


# ---------------------------------------------------------------------------
# Price levels / breakouts / bounces
# ---------------------------------------------------------------------------


def prior_high(high: pd.Series, lookback: int) -> pd.Series:
    """Highest high over the prior ``lookback`` bars (does NOT include current bar).

    Implementation: ``high.rolling(lookback).max().shift(1)``. The warm-up
    region (first ``lookback`` bars) is ``NaN``.
    """
    _ensure_series(high, "high")
    n = _positive_int(lookback, "lookback")
    return cast(pd.Series, high.rolling(n).max().shift(1))


def prior_low(low: pd.Series, lookback: int) -> pd.Series:
    """Lowest low over the prior ``lookback`` bars (does NOT include current bar)."""
    _ensure_series(low, "low")
    n = _positive_int(lookback, "lookback")
    return cast(pd.Series, low.rolling(n).min().shift(1))


def broke_above(series: pd.Series, level: LevelLike) -> pd.Series:
    """Bar-wise True where ``series`` crossed up through ``level``.

    ``level`` may be a Series (compared bar-by-bar, using the previous-bar
    level for the "was below" half of the check) or a scalar (broadcast).
    """
    _ensure_series(series, "series")
    _ensure_level(level, "level")
    lvl = _level_to_series(level, series)
    cond = (series.shift(1) <= lvl.shift(1)) & (series > lvl)
    return cast(pd.Series, cond.fillna(False).astype(bool))


def broke_below(series: pd.Series, level: LevelLike) -> pd.Series:
    """Bar-wise True where ``series`` crossed down through ``level``."""
    _ensure_series(series, "series")
    _ensure_level(level, "level")
    lvl = _level_to_series(level, series)
    cond = (series.shift(1) >= lvl.shift(1)) & (series < lvl)
    return cast(pd.Series, cond.fillna(False).astype(bool))


def touched_above(high: pd.Series, level: LevelLike) -> pd.Series:
    """Bar-wise True the first bar ``high`` touches or exceeds ``level``.

    Bar-level "first touch" approximation: ``high >= level`` AND
    ``high.shift(1) < level.shift(1)``.
    """
    _ensure_series(high, "high")
    _ensure_level(level, "level")
    lvl = _level_to_series(level, high)
    cond = (high >= lvl) & (high.shift(1) < lvl.shift(1))
    return cast(pd.Series, cond.fillna(False).astype(bool))


def touched_below(low: pd.Series, level: LevelLike) -> pd.Series:
    """Bar-wise True the first bar ``low`` touches or falls through ``level``."""
    _ensure_series(low, "low")
    _ensure_level(level, "level")
    lvl = _level_to_series(level, low)
    cond = (low <= lvl) & (low.shift(1) > lvl.shift(1))
    return cast(pd.Series, cond.fillna(False).astype(bool))


def bounced_from(
    low: pd.Series,
    close: pd.Series,
    support: LevelLike,
    tol: float = 0.01,
) -> pd.Series:
    """Bar-wise True where price dipped to support (within tol) and closed above it.

    Definition: ``(low <= support * (1 + tol))`` AND ``(close > support)``.

    ``tol`` is a fractional tolerance (``0.01`` = 1%) and must be ``>= 0``.
    """
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    _ensure_level(support, "support")
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        raise TypeError(
            f"tol must be a number, got {type(tol).__name__}({tol!r})"
        )
    if tol < 0:
        raise ValueError(f"tol must be >= 0, got {tol!r}")
    sup = _level_to_series(support, low)
    cond = (low <= sup * (1.0 + float(tol))) & (close > sup)
    return cast(pd.Series, cond.fillna(False).astype(bool))


# ---------------------------------------------------------------------------
# Causal swing detection
#
# A pivot high at offset ``i`` requires ``high[i] == max(high[i-left :
# i+right+1])``. Because the right-side check needs ``right`` future bars,
# the pivot can only be **known** at index ``i + right``. We therefore stamp
# the boolean output at ``i + right``, not ``i``. This guarantees that
# reading ``swing_high(...).iloc[-1]`` in ``on_bar`` only sees pivots whose
# ``right`` confirming bars have already arrived — no lookahead.
# ---------------------------------------------------------------------------


def _swing_mask(
    series: pd.Series, left: int, right: int, *, find_max: bool
) -> pd.Series:
    """Internal: causal pivot detection on ``series``.

    Vectorised via ``rolling(window=left+right+1, center=True)``. ``center``
    aligns the rolling window so that index ``i`` sees
    ``series[i-left : i+right+1]`` (only possible because pandas can index
    into the future of the series — but we then ``.shift(right)`` so the
    True is **stamped at the confirmation bar** ``i + right``, not ``i``.
    The first ``left + right`` bars of the output are therefore ``False``
    (no pivot has been confirmed yet).
    """
    l = _non_negative_int(left, "left")
    r = _non_negative_int(right, "right")
    if l + r < 1:
        raise ValueError(
            f"left + right must be >= 1 to define a pivot, got left={l!r}, right={r!r}"
        )
    window = l + r + 1
    if find_max:
        rolled = series.rolling(window=window, center=True).max()
    else:
        rolled = series.rolling(window=window, center=True).min()
    # At position i (after ``center=True``), rolled[i] is max/min over
    # [i-left, i+right]. ``series[i] == rolled[i]`` identifies a pivot at i.
    is_pivot_at_i = series.eq(rolled)
    # Stamp the True at the confirmation bar (i + right). ``shift(right)``
    # moves the value at i forward to i + right. ``fillna(False)`` collapses
    # the warm-up NaNs (the leading ``left`` and trailing ``right`` bars of
    # the rolling output) into False.
    confirmed = is_pivot_at_i.shift(r).fillna(False).astype(bool)
    return cast(pd.Series, confirmed)


def swing_high(high: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    """Causal swing-high detector.

    Marks ``True`` at the **confirmation bar** ``i + right``, where bar ``i``
    is the local maximum over ``high[i - left : i + right + 1]``. Until
    ``right`` future bars have passed the pivot is unknown, so the output
    at any index ``j`` depends only on bars at indices ``<= j``.

    Warm-up: first ``left + right`` bars are ``False``.
    """
    _ensure_series(high, "high")
    return _swing_mask(high, left=left, right=right, find_max=True)


def swing_low(low: pd.Series, left: int = 3, right: int = 3) -> pd.Series:
    """Causal swing-low detector. Mirror of :func:`swing_high`."""
    _ensure_series(low, "low")
    return _swing_mask(low, left=left, right=right, find_max=False)


def last_swing_high_level(
    high: pd.Series, left: int = 3, right: int = 3
) -> pd.Series:
    """At each bar, the price of the most recently **confirmed** swing high.

    At each confirmation bar ``i + right`` we know the pivot's price was
    ``high[i]`` (the pivot bar itself, ``right`` bars in the past). The
    function places that price at index ``i + right`` and forward-fills
    until the next confirmation. Warm-up region is ``NaN``.
    """
    _ensure_series(high, "high")
    r = _non_negative_int(right, "right")
    confirmed = swing_high(high, left=left, right=right)
    # Confirmation at index (i + right): the pivot price was high[i] =
    # high.shift(right)[i + right]. So at the confirmation bars place
    # ``high.shift(right)`` and forward-fill.
    pivot_price = high.shift(r)
    levels = pivot_price.where(confirmed)
    return cast(pd.Series, levels.ffill())


def last_swing_low_level(
    low: pd.Series, left: int = 3, right: int = 3
) -> pd.Series:
    """At each bar, the price of the most recently confirmed swing low."""
    _ensure_series(low, "low")
    r = _non_negative_int(right, "right")
    confirmed = swing_low(low, left=left, right=right)
    pivot_price = low.shift(r)
    levels = pivot_price.where(confirmed)
    return cast(pd.Series, levels.ffill())


# ---------------------------------------------------------------------------
# Structural patterns (built on confirmed pivots only)
#
# Each function iterates through confirmation indices in chronological
# order. When a pattern is detected at the latest confirmation (e.g. second
# peak of a double top), the True is stamped at that same confirmation bar
# — by construction it is causal. Implementations mirror the thresholds of
# ``doyoutrade.api.operations.pattern`` (5% shoulder tolerance, 3% top/bottom
# tolerance, 2% triangle flat-tolerance) but never look forward.
# ---------------------------------------------------------------------------


def _confirmed_pivot_records(
    series: pd.Series, left: int, right: int, *, find_max: bool
) -> list[tuple[int, int, float]]:
    """Internal: list of ``(pivot_index_i, confirmation_index_i+right, value)``.

    Pivots are returned in chronological order of confirmation. The value
    is the price at the original pivot bar ``i``.
    """
    confirmed = _swing_mask(series, left=left, right=right, find_max=find_max)
    values = series.to_numpy(dtype="float64")
    n = len(series)
    records: list[tuple[int, int, float]] = []
    conf_arr = confirmed.to_numpy(dtype=bool)
    for conf_idx in range(n):
        if not conf_arr[conf_idx]:
            continue
        pivot_idx = conf_idx - right
        if pivot_idx < 0:
            # Defensive: shouldn't happen because _swing_mask warm-ups the
            # leading ``left`` bars to False, but stay strict.
            continue
        records.append((pivot_idx, conf_idx, float(values[pivot_idx])))
    return records


def double_top(
    high: pd.Series,
    left: int = 3,
    right: int = 3,
    tol: float = 0.03,
) -> pd.Series:
    """Detect double-top via two consecutive **confirmed** swing highs near same price.

    Definition (mirrors ``pattern.py`` lines 227-233): for any two
    consecutive confirmed peaks with prices ``v1``, ``v2``,
    ``abs(v1 - v2) / mean(v1, v2) < tol`` is a double top. ``True`` is
    stamped at the **confirmation bar of the second peak** so the signal is
    causal.
    """
    _ensure_series(high, "high")
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        raise TypeError(f"tol must be a number, got {type(tol).__name__}({tol!r})")
    if tol < 0:
        raise ValueError(f"tol must be >= 0, got {tol!r}")
    result = pd.Series(False, index=high.index, dtype=bool)
    records = _confirmed_pivot_records(high, left=left, right=right, find_max=True)
    if len(records) < 2:
        return result
    for j in range(len(records) - 1):
        _, _, v1 = records[j]
        _, conf2, v2 = records[j + 1]
        if np.isnan(v1) or np.isnan(v2):
            continue
        avg = (v1 + v2) / 2.0
        if avg == 0:
            continue
        if abs(v1 - v2) / avg < float(tol):
            result.iloc[conf2] = True
    return result


def double_bottom(
    low: pd.Series,
    left: int = 3,
    right: int = 3,
    tol: float = 0.03,
) -> pd.Series:
    """Detect double-bottom via two consecutive confirmed swing lows near same price."""
    _ensure_series(low, "low")
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        raise TypeError(f"tol must be a number, got {type(tol).__name__}({tol!r})")
    if tol < 0:
        raise ValueError(f"tol must be >= 0, got {tol!r}")
    result = pd.Series(False, index=low.index, dtype=bool)
    records = _confirmed_pivot_records(low, left=left, right=right, find_max=False)
    if len(records) < 2:
        return result
    for j in range(len(records) - 1):
        _, _, v1 = records[j]
        _, conf2, v2 = records[j + 1]
        if np.isnan(v1) or np.isnan(v2):
            continue
        avg = (v1 + v2) / 2.0
        if avg == 0:
            continue
        if abs(v1 - v2) / abs(avg) < float(tol):
            result.iloc[conf2] = True
    return result


def head_and_shoulders(
    high: pd.Series,
    left: int = 3,
    right: int = 3,
    shoulder_tol: float = 0.05,
) -> pd.Series:
    """Head-and-shoulders top via three consecutive confirmed swing highs.

    Definition (mirrors ``pattern.py`` lines 199-208): for any three
    consecutive confirmed peaks ``lv < hv > rv`` with
    ``abs(lv - rv) / mean(lv, rv) < shoulder_tol``, mark a H&S at the
    confirmation bar of the **third peak** (the right shoulder is now
    confirmed). Causal because the third peak's confirmation bar is the
    earliest bar at which the right shoulder is known.
    """
    _ensure_series(high, "high")
    if not isinstance(shoulder_tol, (int, float)) or isinstance(shoulder_tol, bool):
        raise TypeError(
            f"shoulder_tol must be a number, got "
            f"{type(shoulder_tol).__name__}({shoulder_tol!r})"
        )
    if shoulder_tol < 0:
        raise ValueError(f"shoulder_tol must be >= 0, got {shoulder_tol!r}")
    result = pd.Series(False, index=high.index, dtype=bool)
    records = _confirmed_pivot_records(high, left=left, right=right, find_max=True)
    if len(records) < 3:
        return result
    for j in range(len(records) - 2):
        _, _, lv = records[j]
        _, _, hv = records[j + 1]
        _, conf3, rv = records[j + 2]
        if any(np.isnan(x) for x in (lv, hv, rv)):
            continue
        if hv <= lv or hv <= rv:
            continue
        avg = (lv + rv) / 2.0
        if avg == 0:
            continue
        if abs(lv - rv) / avg > float(shoulder_tol):
            continue
        result.iloc[conf3] = True
    return result


def triangle(
    high: pd.Series,
    low: pd.Series,
    window: int = 20,
    left: int = 3,
    right: int = 3,
) -> pd.Series:
    """Detect ascending (``+1``) / descending (``-1``) triangles over a rolling window.

    For each bar ``b`` we look at confirmed peaks and confirmed valleys
    whose **confirmation indices** fall inside ``(b - window, b]``. Then:

    - Ascending: valley slope > flat AND |peak slope| < flat → ``+1``.
    - Descending: peak slope < -flat AND |valley slope| < flat → ``-1``.
    - Otherwise: ``0``.

    ``flat`` is ``2% * (max(peak prices) - min(valley prices))``, matching
    ``pattern.py`` line 281. Because only confirmed pivots ever enter the
    fit, the output at any bar depends only on past bars.
    """
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    w = _positive_int(window, "window")
    n = len(high)
    result = pd.Series(0, index=high.index, dtype=int)
    peak_records = _confirmed_pivot_records(high, left=left, right=right, find_max=True)
    valley_records = _confirmed_pivot_records(low, left=left, right=right, find_max=False)
    if not peak_records or not valley_records:
        return result
    # peak_records[j] = (pivot_i, confirmation_idx, value). Sort by confirmation idx.
    peak_records.sort(key=lambda r: r[1])
    valley_records.sort(key=lambda r: r[1])
    peak_confs = np.array([r[1] for r in peak_records], dtype=np.int64)
    peak_vals = np.array([r[2] for r in peak_records], dtype=np.float64)
    valley_confs = np.array([r[1] for r in valley_records], dtype=np.int64)
    valley_vals = np.array([r[2] for r in valley_records], dtype=np.float64)
    for b in range(w, n):
        lo_cut = b - w
        # Peaks with lo_cut < confirmation_idx <= b
        pmask = (peak_confs > lo_cut) & (peak_confs <= b)
        vmask = (valley_confs > lo_cut) & (valley_confs <= b)
        if pmask.sum() < 2 or vmask.sum() < 2:
            continue
        pvals = peak_vals[pmask]
        vvals = valley_vals[vmask]
        if np.any(np.isnan(pvals)) or np.any(np.isnan(vvals)):
            continue
        rng = float(pvals.max() - vvals.min())
        if rng == 0:
            continue
        flat = rng * 0.02
        ps = float(np.polyfit(np.arange(len(pvals), dtype=float), pvals, 1)[0])
        vs = float(np.polyfit(np.arange(len(vvals), dtype=float), vvals, 1)[0])
        if vs > flat and abs(ps) < flat:
            result.iloc[b] = 1
        elif ps < -flat and abs(vs) < flat:
            result.iloc[b] = -1
    return result


def broadening(
    high: pd.Series,
    low: pd.Series,
    window: int = 20,
    left: int = 3,
    right: int = 3,
) -> pd.Series:
    """Detect broadening (megaphone) patterns over a rolling window.

    At each bar ``b``, consider confirmed peaks and confirmed valleys whose
    confirmation indices fall inside ``(b - window, b]``. ``True`` if the
    peak sequence is strictly rising **and** the valley sequence is strictly
    falling. Causal: only confirmed pivots are considered.
    """
    _ensure_series(high, "high")
    _ensure_series(low, "low")
    w = _positive_int(window, "window")
    n = len(high)
    result = pd.Series(False, index=high.index, dtype=bool)
    peak_records = _confirmed_pivot_records(high, left=left, right=right, find_max=True)
    valley_records = _confirmed_pivot_records(low, left=left, right=right, find_max=False)
    if not peak_records or not valley_records:
        return result
    peak_records.sort(key=lambda r: r[1])
    valley_records.sort(key=lambda r: r[1])
    peak_confs = np.array([r[1] for r in peak_records], dtype=np.int64)
    peak_vals = np.array([r[2] for r in peak_records], dtype=np.float64)
    valley_confs = np.array([r[1] for r in valley_records], dtype=np.int64)
    valley_vals = np.array([r[2] for r in valley_records], dtype=np.float64)
    for b in range(w, n):
        lo_cut = b - w
        pmask = (peak_confs > lo_cut) & (peak_confs <= b)
        vmask = (valley_confs > lo_cut) & (valley_confs <= b)
        if pmask.sum() < 2 or vmask.sum() < 2:
            continue
        pvals = peak_vals[pmask]
        vvals = valley_vals[vmask]
        if np.any(np.isnan(pvals)) or np.any(np.isnan(vvals)):
            continue
        peaks_rising = bool(np.all(np.diff(pvals) > 0))
        valleys_falling = bool(np.all(np.diff(vvals) < 0))
        if peaks_rising and valleys_falling:
            result.iloc[b] = True
    return result


__all__ = [
    "bounced_from",
    "broadening",
    "broke_above",
    "broke_below",
    "double_bottom",
    "double_top",
    "head_and_shoulders",
    "is_bearish_engulfing",
    "is_bearish_harami",
    "is_bullish_engulfing",
    "is_bullish_harami",
    "is_doji",
    "is_hammer",
    "is_inverted_hammer",
    "last_swing_high_level",
    "last_swing_low_level",
    "prior_high",
    "prior_low",
    "swing_high",
    "swing_low",
    "touched_above",
    "touched_below",
    "triangle",
]
