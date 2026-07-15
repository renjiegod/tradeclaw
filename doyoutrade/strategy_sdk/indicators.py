"""Vetted technical indicators for ``Strategy`` strategy code.

This module is the **single source of truth** for the classic indicator
formulas inside authored strategies. Hand-rolled ``close.ewm(...).mean()``
chains that re-implement MACD / RSI / ADX / Bollinger / ATR are discouraged
in two ways:

- An author who re-implements these is one typo away from a silent bug
  (e.g. ``signal_line = ema_fast.ewm(...).mean()`` instead of
  ``macd_line.ewm(...)``). The runtime can't tell that's wrong; the
  backtest just produces "looks fine but wrong" numbers.
- The ``required_history`` attribute is computed against the formula's
  warm-up length. When the formula moves into the strategy body, the
  attribute and the formula drift out of sync — the runner provisions
  fewer bars than the indicator needs and ``has_required_history`` filters
  every cycle out without an error.

All functions accept ``pandas.Series`` (and ``pandas.DataFrame`` columns)
and return Series, or a ``NamedTuple`` of Series for multi-output
indicators. Outputs are index-aligned with the input. Warm-up bars produce
``NaN`` — always gate ``.iloc[-1]`` reads through
``startup_history`` (see ``Strategy``).

The ``signal_from(...)`` helper exists specifically to make the
"position-naive target state" contract explicit at the call site: it lifts
a boolean condition into the ``{0, 1}`` value ``generate`` must return,
so authors stop accidentally encoding "today an event happened" as
"target state for today" — the failure mode that the MACD-cross-only
strategy hit in production.

Required-history hints (include warm-up margin):

- ``sma(close, n)``                      → ``n``
- ``ema(close, span)``                   → ``span * 4``  (EWM convergence)
- ``macd(close, fast, slow, signal)``    → ``slow + signal + slow * 3``
- ``rsi(close, period)``                 → ``period * 4``
- ``adx(high, low, close, period)``      → ``period * 4``
- ``bollinger(close, window, num_std)``  → ``window``
- ``atr(high, low, close, period)``      → ``period * 4``
- ``obv(close, volume)``                 → ``2`` (uses ``.diff()``)
- ``kdj(high, low, close, n, k, d)``     → ``n + (k + d) * 4`` (rolling + EWM)
- ``williams_r(high, low, close, p)``    → ``period``
- ``cci(high, low, close, period)``      → ``period``
- ``roc(close, period)``                 → ``period + 1``
- ``momentum(close, period)``            → ``period + 1``
- ``mfi(high, low, close, vol, period)`` → ``period + 1``
- ``trix(close, period)``                → ``period * 3 * 4 + 1`` (triple EWM)
- ``vwap(high, low, close, vol, window)``→ ``window`` (``1`` when anchored)
- ``cmf(high, low, close, vol, period)`` → ``period``
- ``ad(high, low, close, volume)``       → ``2`` (cumulative)
- ``volume_ratio(volume, window)``       → ``window``
- ``keltner(h, l, c, ema_w, atr_p, m)``  → ``max(ema_w * 4, atr_p * 4)``
- ``donchian(high, low, window)``        → ``window``
- ``stdev(close, window)``               → ``window``
- ``hist_volatility(close, window, ppy)``→ ``window + 1``
- ``wma(close, window)``                 → ``window``
- ``dema(close, span)``                  → ``span * 4``
- ``kama(close, period, fast, slow)``    → ``period * 4`` (adaptive seed)
- ``supertrend(h, l, c, period, mult)``  → ``period * 4``
- ``psar(high, low, step, max_step)``    → ``5`` (iterative; needs a trend)
- ``ichimoku(h, l, c, ten, kij, sb)``    → ``senkou_b + kijun``
- ``zigzag(close, threshold)``           → data-dependent; enough bars for
  one ``threshold``-sized round trip (larger threshold needs more)
- ``limit_up_approx(close, high, symbol=...)`` → ``2`` (uses prior close)
- ``limit_down_approx(close, low, symbol=...)`` → ``2`` (uses prior close)
"""

from __future__ import annotations

from typing import NamedTuple, Union, cast

import numpy as np
import pandas as pd


class MACDResult(NamedTuple):
    """Output of :func:`macd`.

    - ``macd``: MACD line = ``EMA(close, fast) - EMA(close, slow)``
    - ``signal``: signal line = ``EMA(MACD, signal_span)``
    - ``hist``: histogram = ``macd - signal`` (a positive last value means
      "MACD above signal line" → target_state long for a classic MACD trend
      strategy; pass it through :func:`signal_from`).
    """

    macd: pd.Series
    signal: pd.Series
    hist: pd.Series


class BollingerResult(NamedTuple):
    """Output of :func:`bollinger`. ``upper / middle / lower`` are Series."""

    upper: pd.Series
    middle: pd.Series
    lower: pd.Series


class ADXResult(NamedTuple):
    """Output of :func:`adx`.

    - ``adx``: Wilder-smoothed DX (direction-agnostic strength).
    - ``plus_di`` / ``minus_di``: directional indicators; cross over
      ``minus_di`` indicates bullish.
    """

    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


class KDJResult(NamedTuple):
    """Output of :func:`kdj` (A-share KDJ stochastic).

    - ``k``: smoothed RSV (fast stochastic).
    - ``d``: smoothed ``k`` (slow stochastic).
    - ``j``: ``3 * k - 2 * d`` — the divergence line. ``j > 100`` is the
      classic overbought read, ``j < 0`` oversold. Compare *levels* and
      route through :func:`signal_from`, not the cross event.
    """

    k: pd.Series
    d: pd.Series
    j: pd.Series


class KeltnerResult(NamedTuple):
    """Output of :func:`keltner`.

    ``upper / middle / lower`` are Series: the EMA midline plus/minus
    ``multiplier`` * ATR.
    """

    upper: pd.Series
    middle: pd.Series
    lower: pd.Series


class DonchianResult(NamedTuple):
    """Output of :func:`donchian`.

    - ``upper``: rolling highest high.
    - ``lower``: rolling lowest low.
    - ``middle``: midpoint ``(upper + lower) / 2``.
    """

    upper: pd.Series
    middle: pd.Series
    lower: pd.Series


class SuperTrendResult(NamedTuple):
    """Output of :func:`supertrend`.

    - ``supertrend``: the trailing-stop line (ATR band that flips sides).
    - ``direction``: ``+1`` while price is in an up-trend (line *below*
      price), ``-1`` while in a down-trend (line *above* price). Read
      ``direction.iloc[-1] > 0`` as the long target_state through
      :func:`signal_from`.
    """

    supertrend: pd.Series
    direction: pd.Series


class IchimokuResult(NamedTuple):
    """Output of :func:`ichimoku`.

    - ``tenkan``: conversion line (fast midpoint).
    - ``kijun``: base line (slow midpoint).
    - ``senkou_a`` / ``senkou_b``: the two cloud edges, shifted *forward*
      ``kijun`` bars. The value at the current bar is derived from data
      ``kijun`` bars ago — past-derived, **not** look-ahead — so trading
      the current price against this cloud is safe.
    - ``chikou``: lagging span = ``close`` shifted *back* ``kijun`` bars.
      By construction the last ``kijun`` values are ``NaN``; **never** use
      ``chikou`` to derive a current-bar signal — mid-series it references
      future closes and exists for chart display / offline study only.
    """

    tenkan: pd.Series
    kijun: pd.Series
    senkou_a: pd.Series
    senkou_b: pd.Series
    chikou: pd.Series


class ZigZagResult(NamedTuple):
    """Output of :func:`zigzag` (percent-reversal swing filter).

    - ``pivot``: the confirmed swing-extreme price placed at the bar where
      that extreme occurred (``NaN`` on every non-pivot bar). **Repaints on
      the active leg**: the running extreme of the leg in progress is *not*
      yet a pivot — it only becomes one once price reverses by ``threshold``,
      and that confirmation happens on a *later* bar. So a non-NaN
      ``pivot`` value can appear at an index that was ``NaN`` when that bar
      was first processed. Reading ``pivot.iloc[-1]`` therefore peeks at a
      not-yet-confirmed swing; treat ``pivot`` as chart/offline anchors, not
      a live signal.
    - ``direction``: the **confirmed** swing direction per bar — ``+1`` once
      an up-swing from the last low is confirmed, ``-1`` once a down-swing
      from the last high is confirmed, ``NaN`` until the first swing is
      established. This flips *only* at the bar where the ``threshold``
      reversal is breached, using data up to that bar only, so it never
      repaints — ``direction.iloc[-1]`` is the look-ahead-safe read. Route
      ``direction.iloc[-1] > 0`` through :func:`signal_from` for a
      "ride the confirmed swing" target_state.
    """

    pivot: pd.Series
    direction: pd.Series


def _ensure_series(values: object, name: str) -> pd.Series:
    if not isinstance(values, pd.Series):
        raise TypeError(
            f"{name} must be a pandas.Series, got {type(values).__name__}; "
            f"pass df['{name}'] from data_map[symbol]."
        )
    return values


def _positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}({value!r})")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}({value!r})")
    v = float(value)
    if not v > 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return v


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average over a fixed window."""

    _ensure_series(close, "close")
    n = _positive_int(window, "sma window")
    return cast(pd.Series, close.rolling(n).mean())


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average using ``ewm(span=N, adjust=False)``.

    Matches the standard "MACD-style" EMA seed (recursive, not SMA-seeded).
    """

    _ensure_series(close, "close")
    s = _positive_int(span, "ema span")
    return cast(pd.Series, close.ewm(span=s, adjust=False).mean())


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """MACD trio (classic 12/26/9 defaults).

    Returns :class:`MACDResult` ``(macd, signal, hist)``. The histogram
    sign — not the cross event — is the standard *target_state* read:
    ``hist > 0`` means "MACD above signal line, want long". Use
    :func:`signal_from` to lift it into ``{0, 1}``.
    """

    _ensure_series(close, "close")
    f = _positive_int(fast, "macd fast")
    sl = _positive_int(slow, "macd slow")
    sg = _positive_int(signal, "macd signal")
    if f >= sl:
        raise ValueError(
            f"macd fast ({f}) must be smaller than slow ({sl}); "
            "otherwise the MACD line degenerates."
        )
    ema_fast = ema(close, f)
    ema_slow = ema(close, sl)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=sg, adjust=False).mean()
    hist = macd_line - signal_line
    return MACDResult(macd=macd_line, signal=signal_line, hist=hist)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI using ``ewm(alpha=1/period, adjust=False)``.

    The Wilder smoothing (not a rolling SMA of gains/losses) is the
    convention every other indicator library follows; switching to a
    plain rolling mean produces a different curve on short windows and
    causes ports of strategies from one platform to another to disagree.
    """

    _ensure_series(close, "close")
    p = _positive_int(period, "rsi period")
    delta = close.diff()
    gain = cast(pd.Series, delta.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean())
    loss = cast(
        pd.Series,
        (-delta.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean(),
    )
    rs = gain / loss.replace(0, np.nan)
    out = cast(pd.Series, 100 - 100 / (1 + rs))
    # All-gain branch (loss == 0, gain > 0) is RSI = 100 by convention.
    out = cast(pd.Series, out.where(~(loss.eq(0) & gain.gt(0)), 100.0))
    # All-flat branch (gain == 0 and loss == 0) → NaN propagates; that is
    # the standard "undefined" reading and callers should treat it as
    # "no signal" by gating on has_required_history + .iloc[-1] checks.
    return out


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> ADXResult:
    """Wilder ADX / +DI / -DI."""

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    p = _positive_int(period, "adx period")
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm_raw = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm_raw = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / p, adjust=False).mean()
    plus_di = 100 * plus_dm_raw.ewm(alpha=1 / p, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm_raw.ewm(alpha=1 / p, adjust=False).mean() / atr_.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_line = dx.ewm(alpha=1 / p, adjust=False).mean()
    return ADXResult(adx=adx_line, plus_di=plus_di, minus_di=minus_di)


def bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> BollingerResult:
    """Bollinger Bands; population stdev (``ddof=0``)."""

    _ensure_series(close, "close")
    n = _positive_int(window, "bollinger window")
    k = float(num_std)
    if k <= 0:
        raise ValueError(f"bollinger num_std must be > 0, got {num_std!r}")
    middle = cast(pd.Series, close.rolling(n).mean())
    std = cast(pd.Series, close.rolling(n).std(ddof=0))
    return BollingerResult(
        upper=cast(pd.Series, middle + k * std),
        middle=middle,
        lower=cast(pd.Series, middle - k * std),
    )


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Wilder ATR (EWM of true range with ``alpha=1/period``)."""

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    p = _positive_int(period, "atr period")
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return cast(pd.Series, tr.ewm(alpha=1 / p, adjust=False).mean())


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume: cumulative ``sign(close.diff()) * volume``."""

    _ensure_series(close, "close")
    _ensure_series(volume, "volume")
    direction = pd.Series(
        np.sign(close.diff().to_numpy()),
        index=close.index,
        dtype="float64",
    ).fillna(0.0)
    return cast(pd.Series, (volume.astype(float) * direction).cumsum())


# --- Momentum / overbought-oversold -----------------------------------------


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    n: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> KDJResult:
    """A-share KDJ stochastic oscillator.

    ``RSV = (close - LL_n) / (HH_n - LL_n) * 100`` over an ``n``-bar window,
    then ``k = EWM(RSV, alpha=1/k_smooth)`` and ``d = EWM(k, alpha=1/d_smooth)``
    (the recursive ``2/3 * prev + 1/3 * new`` smoothing the A-share KDJ uses),
    and ``j = 3*k - 2*d``. Flat windows (``HH == LL``) propagate ``NaN``.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    nn = _positive_int(n, "kdj n")
    ks = _positive_int(k_smooth, "kdj k_smooth")
    ds = _positive_int(d_smooth, "kdj d_smooth")
    lowest = low.rolling(nn).min()
    highest = high.rolling(nn).max()
    span = (highest - lowest).replace(0, np.nan)
    rsv = (close - lowest) / span * 100.0
    k = rsv.ewm(alpha=1 / ks, adjust=False).mean()
    d = k.ewm(alpha=1 / ds, adjust=False).mean()
    j = 3 * k - 2 * d
    return KDJResult(
        k=cast(pd.Series, k),
        d=cast(pd.Series, d),
        j=cast(pd.Series, j),
    )


def williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Williams %R: ``(HH - close) / (HH - LL) * -100`` over ``period`` bars.

    Ranges in ``[-100, 0]``; below ``-80`` is the classic oversold read,
    above ``-20`` overbought. Flat windows propagate ``NaN``.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    p = _positive_int(period, "williams_r period")
    highest = high.rolling(p).max()
    lowest = low.rolling(p).min()
    span = (highest - lowest).replace(0, np.nan)
    return cast(pd.Series, (highest - close) / span * -100.0)


def cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Commodity Channel Index.

    ``TP = (high + low + close) / 3``;
    ``CCI = (TP - SMA(TP, n)) / (0.015 * mean_abs_dev(TP, n))`` where the
    mean absolute deviation is the rolling mean of ``|TP - SMA(TP)|``
    (Lambert's original definition). Flat windows propagate ``NaN``.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    p = _positive_int(period, "cci period")
    tp = (high + low + close) / 3.0
    sma_tp = tp.rolling(p).mean()
    mean_dev = tp.rolling(p).apply(
        lambda window: np.abs(window - window.mean()).mean(), raw=True
    )
    denom = (0.015 * mean_dev).replace(0, np.nan)
    return cast(pd.Series, (tp - sma_tp) / denom)


def roc(close: pd.Series, period: int = 12) -> pd.Series:
    """Rate of change: ``(close / close.shift(period) - 1) * 100`` (percent)."""

    _ensure_series(close, "close")
    p = _positive_int(period, "roc period")
    prev = close.shift(p).replace(0, np.nan)
    return cast(pd.Series, (close / prev - 1.0) * 100.0)


def momentum(close: pd.Series, period: int = 10) -> pd.Series:
    """Momentum: ``close - close.shift(period)`` (absolute price difference)."""

    _ensure_series(close, "close")
    p = _positive_int(period, "momentum period")
    return cast(pd.Series, close - close.shift(p))


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Money Flow Index — the volume-weighted RSI.

    ``TP = (high + low + close) / 3``; raw money flow ``= TP * volume`` is
    split into positive / negative buckets by the sign of ``TP.diff()``,
    summed over ``period`` bars, and folded into ``100 - 100/(1 + ratio)``.
    All-positive windows saturate at 100 (matching :func:`rsi`).
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    _ensure_series(volume, "volume")
    p = _positive_int(period, "mfi period")
    tp = (high + low + close) / 3.0
    raw_flow = tp * volume.astype(float)
    tp_delta = tp.diff()
    pos_flow = raw_flow.where(tp_delta > 0, 0.0)
    neg_flow = raw_flow.where(tp_delta < 0, 0.0)
    pos_sum = pos_flow.rolling(p).sum()
    neg_sum = neg_flow.rolling(p).sum()
    ratio = pos_sum / neg_sum.replace(0, np.nan)
    out = 100.0 - 100.0 / (1.0 + ratio)
    # neg_sum == 0 with positive inflow → MFI 100 by convention.
    out = out.where(~(neg_sum.eq(0) & pos_sum.gt(0)), 100.0)
    return cast(pd.Series, out)


def trix(close: pd.Series, period: int = 15) -> pd.Series:
    """TRIX: percent rate-of-change of a triple-EMA-smoothed close.

    ``EMA`` applied three times (each ``span=period``), then
    ``(triple / triple.shift(1) - 1) * 100``. Oscillates around zero; a
    positive value with rising slope is the classic long read.
    """

    _ensure_series(close, "close")
    p = _positive_int(period, "trix period")
    e1 = close.ewm(span=p, adjust=False).mean()
    e2 = e1.ewm(span=p, adjust=False).mean()
    e3 = e2.ewm(span=p, adjust=False).mean()
    prev = e3.shift(1)
    return cast(pd.Series, (e3 / prev - 1.0) * 100.0)


# --- Volume / price-volume ---------------------------------------------------


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    window: int | None = 14,
) -> pd.Series:
    """Volume-weighted average price of the typical price ``(H+L+C)/3``.

    With ``window`` set, a rolling VWAP over the last ``window`` bars; with
    ``window=None`` an anchored cumulative VWAP from the first bar. Zero
    rolling/cumulative volume propagates ``NaN``.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    _ensure_series(volume, "volume")
    tp = (high + low + close) / 3.0
    vol = volume.astype(float)
    pv = tp * vol
    if window is None:
        num = cast(pd.Series, pv.cumsum())
        den = cast(pd.Series, vol.cumsum())
    else:
        w = _positive_int(window, "vwap window")
        num = cast(pd.Series, pv.rolling(w).sum())
        den = cast(pd.Series, vol.rolling(w).sum())
    return cast(pd.Series, num / den.replace(0, np.nan))


def cmf(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Chaikin Money Flow.

    Money-flow multiplier ``((C-L) - (H-C)) / (H-L)`` (zero on flat bars)
    times volume, summed over ``period`` and divided by summed volume.
    Ranges in ``[-1, 1]``; persistently positive is accumulation.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    _ensure_series(volume, "volume")
    p = _positive_int(period, "cmf period")
    hl = (high - low).replace(0, np.nan)
    mfm = (((close - low) - (high - close)) / hl).fillna(0.0)
    vol = volume.astype(float)
    mfv = mfm * vol
    den = cast(pd.Series, vol.rolling(p).sum()).replace(0, np.nan)
    return cast(pd.Series, cast(pd.Series, mfv.rolling(p).sum()) / den)


def ad(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Accumulation/Distribution line: cumulative money-flow volume.

    Money-flow multiplier ``((C-L) - (H-C)) / (H-L)`` (zero on flat bars)
    times volume, then cumulatively summed.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    _ensure_series(volume, "volume")
    hl = (high - low).replace(0, np.nan)
    mfm = (((close - low) - (high - close)) / hl).fillna(0.0)
    mfv = mfm * volume.astype(float)
    return cast(pd.Series, mfv.cumsum())


def volume_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    """Volume ratio: current volume / rolling mean volume over ``window``.

    A value ``> 1`` means today trades heavier than its ``window``-bar
    average. Zero average volume propagates ``NaN``.
    """

    _ensure_series(volume, "volume")
    w = _positive_int(window, "volume_ratio window")
    vol = volume.astype(float)
    avg = cast(pd.Series, vol.rolling(w).mean()).replace(0, np.nan)
    return cast(pd.Series, vol / avg)


# --- Channel / volatility ----------------------------------------------------


def keltner(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_window: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> KeltnerResult:
    """Keltner Channels: EMA midline ± ``multiplier`` * ATR."""

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    ew = _positive_int(ema_window, "keltner ema_window")
    ap = _positive_int(atr_period, "keltner atr_period")
    m = _positive_float(multiplier, "keltner multiplier")
    middle = ema(close, ew)
    band = atr(high, low, close, ap)
    return KeltnerResult(
        upper=cast(pd.Series, middle + m * band),
        middle=middle,
        lower=cast(pd.Series, middle - m * band),
    )


def donchian(high: pd.Series, low: pd.Series, window: int = 20) -> DonchianResult:
    """Donchian Channels: rolling highest high / lowest low and midpoint."""

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    w = _positive_int(window, "donchian window")
    upper = cast(pd.Series, high.rolling(w).max())
    lower = cast(pd.Series, low.rolling(w).min())
    middle = cast(pd.Series, (upper + lower) / 2.0)
    return DonchianResult(upper=upper, middle=middle, lower=lower)


def stdev(close: pd.Series, window: int = 20) -> pd.Series:
    """Rolling population standard deviation (``ddof=0``, matches :func:`bollinger`)."""

    _ensure_series(close, "close")
    w = _positive_int(window, "stdev window")
    return cast(pd.Series, close.rolling(w).std(ddof=0))


def hist_volatility(
    close: pd.Series, window: int = 20, periods_per_year: int = 252
) -> pd.Series:
    """Annualised historical volatility.

    Rolling population stdev (``ddof=0``) of log returns
    ``ln(close / close.shift(1))`` scaled by ``sqrt(periods_per_year)``.
    """

    _ensure_series(close, "close")
    w = _positive_int(window, "hist_volatility window")
    ppy = _positive_int(periods_per_year, "hist_volatility periods_per_year")
    log_ret = np.log(close / close.shift(1))
    vol = log_ret.rolling(w).std(ddof=0) * np.sqrt(ppy)
    return cast(pd.Series, vol)


# --- Trend (advanced) --------------------------------------------------------


def wma(close: pd.Series, window: int) -> pd.Series:
    """Linearly weighted moving average (weights ``1..window``)."""

    _ensure_series(close, "close")
    w = _positive_int(window, "wma window")
    weights = np.arange(1, w + 1, dtype="float64")
    denom = float(weights.sum())
    return cast(
        pd.Series,
        close.rolling(w).apply(lambda x: float(np.dot(x, weights)) / denom, raw=True),
    )


def dema(close: pd.Series, span: int) -> pd.Series:
    """Double exponential moving average: ``2*EMA - EMA(EMA)``."""

    _ensure_series(close, "close")
    s = _positive_int(span, "dema span")
    e1 = close.ewm(span=s, adjust=False).mean()
    e2 = e1.ewm(span=s, adjust=False).mean()
    return cast(pd.Series, 2 * e1 - e2)


def kama(
    close: pd.Series,
    period: int = 10,
    fast: int = 2,
    slow: int = 30,
) -> pd.Series:
    """Kaufman Adaptive Moving Average.

    Efficiency ratio
    ``ER = |close - close.shift(period)| / sum(|close.diff()|, period)``
    scales a smoothing constant between the ``fast`` and ``slow`` EMA
    bounds; ``KAMA_t = KAMA_{t-1} + SC^2 * (close_t - KAMA_{t-1})``. The
    first ``period`` bars are ``NaN``; the seed is ``close`` at bar
    ``period``.
    """

    _ensure_series(close, "close")
    p = _positive_int(period, "kama period")
    f = _positive_int(fast, "kama fast")
    sl = _positive_int(slow, "kama slow")
    if f >= sl:
        raise ValueError(f"kama fast ({f}) must be smaller than slow ({sl})")
    fast_sc = 2.0 / (f + 1.0)
    slow_sc = 2.0 / (sl + 1.0)
    prices = close.to_numpy(dtype="float64")
    size = prices.size
    out = np.full(size, np.nan, dtype="float64")
    if size <= p:
        return pd.Series(out, index=close.index)
    change = np.abs(prices[p:] - prices[:-p])
    abs_diff = np.abs(np.diff(prices, prepend=prices[0]))
    csum = np.cumsum(abs_diff)
    out[p] = prices[p]
    for i in range(p + 1, size):
        vol = csum[i] - csum[i - p]
        er = 0.0 if (vol == 0 or np.isnan(vol)) else change[i - p] / vol
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        out[i] = out[i - 1] + sc * (prices[i] - out[i - 1])
    return pd.Series(out, index=close.index)


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 10,
    multiplier: float = 3.0,
) -> SuperTrendResult:
    """SuperTrend trailing-stop indicator.

    Basic bands ``(H+L)/2 ± multiplier * ATR(period)`` are made "sticky"
    (the final upper band only ratchets down, the lower band only ratchets
    up) and the trend flips when ``close`` pierces the active band.
    ``direction`` is ``+1`` in an up-trend (line below price), ``-1`` in a
    down-trend.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    p = _positive_int(period, "supertrend period")
    m = _positive_float(multiplier, "supertrend multiplier")
    band = atr(high, low, close, p).to_numpy(dtype="float64")
    hl2 = ((high + low) / 2.0).to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    size = c.size
    basic_upper = hl2 + m * band
    basic_lower = hl2 - m * band
    final_upper = np.full(size, np.nan, dtype="float64")
    final_lower = np.full(size, np.nan, dtype="float64")
    st = np.full(size, np.nan, dtype="float64")
    direction = np.full(size, np.nan, dtype="float64")
    start = None
    for i in range(size):
        if not np.isnan(band[i]):
            start = i
            break
    if start is None:
        return SuperTrendResult(
            supertrend=pd.Series(st, index=close.index),
            direction=pd.Series(direction, index=close.index),
        )
    final_upper[start] = basic_upper[start]
    final_lower[start] = basic_lower[start]
    st[start] = final_upper[start]
    direction[start] = -1.0
    for i in range(start + 1, size):
        final_upper[i] = (
            basic_upper[i]
            if (basic_upper[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower[i]
            if (basic_lower[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
        if st[i - 1] == final_upper[i - 1]:
            if c[i] > final_upper[i]:
                st[i] = final_lower[i]
                direction[i] = 1.0
            else:
                st[i] = final_upper[i]
                direction[i] = -1.0
        else:
            if c[i] < final_lower[i]:
                st[i] = final_upper[i]
                direction[i] = -1.0
            else:
                st[i] = final_lower[i]
                direction[i] = 1.0
    return SuperTrendResult(
        supertrend=pd.Series(st, index=close.index),
        direction=pd.Series(direction, index=close.index),
    )


def psar(
    high: pd.Series,
    low: pd.Series,
    step: float = 0.02,
    max_step: float = 0.2,
) -> pd.Series:
    """Parabolic SAR (Wilder).

    Iterative stop-and-reverse: the SAR accelerates toward price by
    ``step`` each time a new extreme point is made, capped at ``max_step``.
    Returns the SAR level per bar; price crossing the SAR is the reversal
    signal. The first bar seeds an assumed up-trend.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    af_step = _positive_float(step, "psar step")
    af_max = _positive_float(max_step, "psar max_step")
    if af_step > af_max:
        raise ValueError(f"psar step ({af_step}) must be <= max_step ({af_max})")
    highs = high.to_numpy(dtype="float64")
    lows = low.to_numpy(dtype="float64")
    size = highs.size
    out = np.full(size, np.nan, dtype="float64")
    if size < 2:
        return pd.Series(out, index=high.index)
    uptrend = True
    af = af_step
    ep = highs[0]
    sar = lows[0]
    out[0] = sar
    for i in range(1, size):
        sar = sar + af * (ep - sar)
        if uptrend:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < sar:
                uptrend = False
                sar = ep
                ep = lows[i]
                af = af_step
            elif highs[i] > ep:
                ep = highs[i]
                af = min(af + af_step, af_max)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > sar:
                uptrend = True
                sar = ep
                ep = highs[i]
                af = af_step
            elif lows[i] < ep:
                ep = lows[i]
                af = min(af + af_step, af_max)
        out[i] = sar
    return pd.Series(out, index=high.index)


def ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> IchimokuResult:
    """Ichimoku Kinko Hyo.

    See :class:`IchimokuResult` for the look-ahead contract: ``senkou_a`` /
    ``senkou_b`` are shifted *forward* ``kijun`` bars (past-derived, safe to
    read at the current bar); ``chikou`` is shifted *back* ``kijun`` bars
    and must never feed a current-bar signal.
    """

    _ensure_series(high, "high")
    _ensure_series(low, "low")
    _ensure_series(close, "close")
    t = _positive_int(tenkan, "ichimoku tenkan")
    k = _positive_int(kijun, "ichimoku kijun")
    sb = _positive_int(senkou_b, "ichimoku senkou_b")
    tenkan_line = (high.rolling(t).max() + low.rolling(t).min()) / 2.0
    kijun_line = (high.rolling(k).max() + low.rolling(k).min()) / 2.0
    senkou_a = ((tenkan_line + kijun_line) / 2.0).shift(k)
    senkou_b_line = ((high.rolling(sb).max() + low.rolling(sb).min()) / 2.0).shift(k)
    chikou = close.shift(-k)
    return IchimokuResult(
        tenkan=cast(pd.Series, tenkan_line),
        kijun=cast(pd.Series, kijun_line),
        senkou_a=cast(pd.Series, senkou_a),
        senkou_b=cast(pd.Series, senkou_b_line),
        chikou=cast(pd.Series, chikou),
    )


def zigzag(close: pd.Series, threshold: float = 0.05) -> ZigZagResult:
    """ZigZag percent-reversal swing filter.

    Walks ``close`` bar by bar tracking the running extreme of the current
    swing. A new swing is **confirmed** only when price retraces from that
    extreme by at least ``threshold`` (a fraction, e.g. ``0.05`` = 5%); at
    that point the prior extreme is stamped into ``pivot`` at its own bar
    and the swing ``direction`` flips. See :class:`ZigZagResult` for the
    repaint / look-ahead contract (``pivot`` repaints, ``direction`` does
    not).

    ``threshold`` must satisfy ``0 < threshold < 1``: a value ``>= 1`` would
    make the down-reversal trigger ``price * (1 - threshold)`` non-positive
    and silently disable all downward reversals, so it is rejected rather
    than tolerated.

    History needed is data-dependent, not a fixed window: provision enough
    bars to contain at least one full ``threshold``-sized round trip (a
    larger ``threshold`` needs more bars). Until the first swing confirms,
    both outputs stay ``NaN`` — gate reads through ``startup_history``.
    """

    _ensure_series(close, "close")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TypeError(
            f"zigzag threshold must be a number, got "
            f"{type(threshold).__name__}({threshold!r})"
        )
    thr = float(threshold)
    if not 0.0 < thr < 1.0:
        raise ValueError(
            f"zigzag threshold must satisfy 0 < threshold < 1, got {threshold!r}"
        )

    prices = close.to_numpy(dtype="float64")
    size = prices.size
    pivot = np.full(size, np.nan, dtype="float64")
    direction = np.full(size, np.nan, dtype="float64")
    if size == 0:
        return ZigZagResult(
            pivot=pd.Series(pivot, index=close.index),
            direction=pd.Series(direction, index=close.index),
        )

    trend = 0  # 0 = undetermined, +1 = up-swing, -1 = down-swing
    ext_price = prices[0]  # running extreme of the active swing
    ext_idx = 0
    max_price = prices[0]  # running high while trend is undetermined
    max_idx = 0
    min_price = prices[0]  # running low while trend is undetermined
    min_idx = 0
    for i in range(1, size):
        p = prices[i]
        if trend == 1:
            if p > ext_price:
                ext_price = p
                ext_idx = i
            elif p <= ext_price * (1.0 - thr):
                pivot[ext_idx] = ext_price  # confirm the swing high
                trend = -1
                ext_price = p
                ext_idx = i
        elif trend == -1:
            if p < ext_price:
                ext_price = p
                ext_idx = i
            elif p >= ext_price * (1.0 + thr):
                pivot[ext_idx] = ext_price  # confirm the swing low
                trend = 1
                ext_price = p
                ext_idx = i
        else:  # undetermined: whichever threshold breaks first sets the trend
            if p > max_price:
                max_price = p
                max_idx = i
            if p < min_price:
                min_price = p
                min_idx = i
            if p <= max_price * (1.0 - thr):
                pivot[max_idx] = max_price  # the running high was a pivot
                trend = -1
                ext_price = p
                ext_idx = i
            elif p >= min_price * (1.0 + thr):
                pivot[min_idx] = min_price  # the running low was a pivot
                trend = 1
                ext_price = p
                ext_idx = i
        direction[i] = float(trend) if trend != 0 else np.nan
    return ZigZagResult(
        pivot=pd.Series(pivot, index=close.index),
        direction=pd.Series(direction, index=close.index),
    )


def a_share_limit_pct(symbol: str) -> float:
    """Approximate daily limit-up ratio from a canonical A-share symbol.

    Rules (code prefix only — **does not** detect ST/*ST 5% names):

    - Shanghai STAR (688/689) and ChiNext (300–302 SZ): 20%
    - Beijing (.BJ): 30%
    - Other mainland A-share stocks: 10%

    For ST boards pass ``limit_pct=0.05`` to :func:`limit_up_approx` /
    :func:`limit_down_approx`.
    """

    s = (symbol or "").strip().upper()
    if "." not in s:
        return 0.10
    code, suf = s.rsplit(".", 1)
    if suf == "BJ":
        return 0.30
    if len(code) != 6 or not code.isdigit():
        return 0.10
    head3 = int(code[:3])
    if suf == "SH" and head3 in (688, 689):
        return 0.20
    if suf == "SZ" and 300 <= head3 <= 302:
        return 0.20
    return 0.10


def _round_a_share_limit_price(price: float) -> float:
    """Round to the 0.01 tick used for most A-share limit prices."""

    return round(float(price) + 1e-8, 2)


def _resolve_a_share_limit_pct(limit_pct: float | None, symbol: str) -> float:
    if limit_pct is not None:
        pct = float(limit_pct)
        if isinstance(limit_pct, bool) or pct <= 0.0 or pct >= 1.0:
            raise ValueError(
                f"limit_pct must be a number in (0, 1), got {limit_pct!r}"
            )
        return pct
    return a_share_limit_pct(symbol)


def limit_up_approx(
    close: pd.Series,
    high: pd.Series,
    *,
    symbol: str = "",
    prev_close: pd.Series | None = None,
    limit_pct: float | None = None,
    abs_price_tol: float = 0.011,
    close_high_atol: float = 1e-6,
) -> pd.Series:
    """Bar-wise approximate limit-up flag for historical daily backtests.

    A bar is ``True`` when **both**:

    1. ``close`` is within ``abs_price_tol`` of the board's rounded limit-up
       price ``round(prev_close * (1 + limit_pct), 2)`` (``limit_pct`` from
       :func:`a_share_limit_pct` when omitted).
    2. ``close`` equals ``high`` for that bar (within ``close_high_atol``).

    This is an **approximation** for backtests — it does not model intraday
    touch-the-limit-then-fade unless ``close`` still sits at the limit price
    and the day high. ST/*ST (5%) names need an explicit ``limit_pct=0.05``.

    The first bar is always ``False`` (no prior close). Rows with missing
    OHLC are ``False``.
    """

    _close = _ensure_series(close, "close")
    _high = _ensure_series(high, "high")
    if prev_close is not None:
        _prev = _ensure_series(prev_close, "prev_close")
    else:
        _prev = _close.shift(1)

    pct = _resolve_a_share_limit_pct(limit_pct, symbol)
    limit_px = (_prev * (1.0 + pct)).map(_round_a_share_limit_price)
    at_limit = (_close >= limit_px - abs_price_tol) & (_close <= limit_px + abs_price_tol)
    at_high = np.isclose(
        _close.to_numpy(dtype=float),
        _high.to_numpy(dtype=float),
        rtol=0.0,
        atol=close_high_atol,
        equal_nan=False,
    )
    out = at_limit & pd.Series(at_high, index=_close.index)
    valid = _prev.notna() & _close.notna() & _high.notna()
    return cast(pd.Series, out & valid).astype(bool)


def limit_down_approx(
    close: pd.Series,
    low: pd.Series,
    *,
    symbol: str = "",
    prev_close: pd.Series | None = None,
    limit_pct: float | None = None,
    abs_price_tol: float = 0.011,
    close_low_atol: float = 1e-6,
) -> pd.Series:
    """Bar-wise approximate limit-down flag for historical daily backtests.

    A bar is ``True`` when **both**:

    1. ``close`` is within ``abs_price_tol`` of the board's rounded limit-down
       price ``round(prev_close * (1 - limit_pct), 2)`` (``limit_pct`` from
       :func:`a_share_limit_pct` when omitted).
    2. ``close`` equals ``low`` for that bar (within ``close_low_atol``).

    This is an **approximation** for backtests — it does not model intraday
    touch-the-limit-then-bounce unless ``close`` still sits at the limit price
    and the day low. ST/*ST (5%) names need an explicit ``limit_pct=0.05``.

    The first bar is always ``False`` (no prior close). Rows with missing
    OHLC are ``False``.
    """

    _close = _ensure_series(close, "close")
    _low = _ensure_series(low, "low")
    if prev_close is not None:
        _prev = _ensure_series(prev_close, "prev_close")
    else:
        _prev = _close.shift(1)

    pct = _resolve_a_share_limit_pct(limit_pct, symbol)
    limit_px = (_prev * (1.0 - pct)).map(_round_a_share_limit_price)
    at_limit = (_close >= limit_px - abs_price_tol) & (_close <= limit_px + abs_price_tol)
    at_low = np.isclose(
        _close.to_numpy(dtype=float),
        _low.to_numpy(dtype=float),
        rtol=0.0,
        atol=close_low_atol,
        equal_nan=False,
    )
    out = at_limit & pd.Series(at_low, index=_close.index)
    valid = _prev.notna() & _close.notna() & _low.notna()
    return cast(pd.Series, out & valid).astype(bool)


def crossed_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """Bar-wise boolean Series: ``True`` where ``a`` crossed *up* through ``b``.

    Useful for diagnostics (counting events) — but **do not** use this
    directly as a 0/1 target-state signal: a cross only fires on one bar
    and would produce a 1-cycle holding period. For target state, compare
    the *levels* (e.g. ``a > b``) and route through :func:`signal_from`.
    """

    _ensure_series(a, "a")
    _ensure_series(b, "b")
    return (a.shift(1) <= b.shift(1)) & (a > b)


def crossed_below(a: pd.Series, b: pd.Series) -> pd.Series:
    """Bar-wise boolean Series: ``True`` where ``a`` crossed *down* through ``b``.

    See :func:`crossed_above` — same caveat about target state vs event.
    """

    _ensure_series(a, "a")
    _ensure_series(b, "b")
    return (a.shift(1) >= b.shift(1)) & (a < b)


ConditionLike = Union[bool, int, float, np.bool_, pd.Series, None]


def signal_from(condition: ConditionLike) -> int:
    """Lift a boolean condition into the ``{0, 1}`` target-state contract.

    This is the explicit "I am declaring the target state for this cycle"
    converter that ``generate`` should use:

    >>> out[symbol] = indicators.signal_from(macd.hist.iloc[-1] > 0)

    Translation rules:

    - truthy scalar (``True`` / ``1`` / non-zero) → ``1``
    - falsy scalar (``False`` / ``0``)            → ``0``
    - ``None`` or ``NaN``                         → ``0``
    - ``pd.Series`` → uses ``.iloc[-1]``; empty series → ``0``

    Strategies that emit ``1`` *only on the cross bar* and ``0`` everywhere
    else encode an *event*, not a target state, and get diff'd against the
    portfolio every cycle — that's the production bug this helper exists
    to make obvious. Compare *levels* (``a > b``), not *events*
    (``crossed_above``), when filling the output dict.
    """

    if isinstance(condition, pd.Series):
        if condition.empty:
            return 0
        last = condition.iloc[-1]
    else:
        last = condition

    if last is None:
        return 0
    try:
        if isinstance(last, float) and np.isnan(last):
            return 0
    except TypeError:
        pass
    return 1 if bool(last) else 0


__all__ = [
    "ADXResult",
    "BollingerResult",
    "DonchianResult",
    "IchimokuResult",
    "KDJResult",
    "KeltnerResult",
    "MACDResult",
    "SuperTrendResult",
    "ZigZagResult",
    "ad",
    "adx",
    "atr",
    "bollinger",
    "cci",
    "cmf",
    "crossed_above",
    "crossed_below",
    "dema",
    "donchian",
    "ema",
    "hist_volatility",
    "ichimoku",
    "kama",
    "kdj",
    "keltner",
    "a_share_limit_pct",
    "limit_up_approx",
    "limit_down_approx",
    "macd",
    "mfi",
    "momentum",
    "obv",
    "psar",
    "roc",
    "rsi",
    "signal_from",
    "sma",
    "stdev",
    "supertrend",
    "trix",
    "volume_ratio",
    "vwap",
    "wma",
    "williams_r",
    "zigzag",
]
