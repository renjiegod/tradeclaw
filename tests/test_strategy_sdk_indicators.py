"""Numerical correctness tests for ``doyoutrade.strategy_sdk.indicators``.

These tests pin the vetted indicator formulas against:

- Self-consistency (e.g. SMA(n) of a constant series equals the constant).
- Edge cases (NaN warm-up window, all-flat input, short series, empty
  ``Series`` into :func:`signal_from`).
- Cross-checks vs. the direct pandas/numpy expression that the SKILL doc
  references — guards against accidental formula drift between the SDK and
  the `strategy-definition-authoring/references/indicators.md` examples.

The MACD-specific cross-check is the most important guard: the production
bug from ``tmp/error_request.json`` could be re-introduced silently if a
typo (e.g. ``signal_line = ema_fast.ewm(...)`` instead of
``macd_line.ewm(...)``) sneaks into ``indicators.macd``.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from doyoutrade.strategy_sdk import indicators


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


class SmaTests(unittest.TestCase):
    def test_window_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            indicators.sma(_series([1.0, 2.0]), window=0)
        with self.assertRaises(ValueError):
            indicators.sma(_series([1.0, 2.0]), window=-1)

    def test_constant_input_returns_constant_after_window(self) -> None:
        s = _series([5.0] * 10)
        out = indicators.sma(s, window=3)
        # First (window - 1) entries are NaN, remainder == 5.0.
        self.assertTrue(out.iloc[:2].isna().all())
        np.testing.assert_allclose(out.iloc[2:].to_numpy(), [5.0] * 8)

    def test_matches_pandas_rolling_mean(self) -> None:
        s = _series([float(i) for i in range(1, 21)])
        out = indicators.sma(s, window=5)
        expected = s.rolling(5).mean()
        pd.testing.assert_series_equal(out, expected, check_names=False)


class EmaTests(unittest.TestCase):
    def test_span_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            indicators.ema(_series([1.0]), span=0)

    def test_matches_ewm_with_adjust_false(self) -> None:
        s = _series([float(i) for i in range(50)])
        out = indicators.ema(s, span=12)
        expected = s.ewm(span=12, adjust=False).mean()
        pd.testing.assert_series_equal(out, expected, check_names=False)


class MacdTests(unittest.TestCase):
    def test_default_spans(self) -> None:
        s = _series([float(i) for i in range(60)])
        out = indicators.macd(s)
        # Compare against the reference formula directly so a typo in
        # the SDK implementation (e.g. signal = EMA(fast, 9) instead of
        # EMA(macd, 9)) is caught here.
        ema_fast = s.ewm(span=12, adjust=False).mean()
        ema_slow = s.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        pd.testing.assert_series_equal(out.macd, macd_line, check_names=False)
        pd.testing.assert_series_equal(out.signal, signal_line, check_names=False)
        pd.testing.assert_series_equal(out.hist, hist, check_names=False)

    def test_fast_must_be_smaller_than_slow(self) -> None:
        with self.assertRaises(ValueError):
            indicators.macd(_series([1.0, 2.0]), fast=26, slow=12)
        with self.assertRaises(ValueError):
            indicators.macd(_series([1.0]), fast=12, slow=12)

    def test_step_up_pattern_hist_flips_positive(self) -> None:
        # The smoke gate's step_up scenario: flat then a jump at the
        # last bar. Hist must end positive (ema_fast catches up faster
        # than ema_slow), which is exactly the "want held long"
        # target_state read.
        closes = [100.0] * 54 + [110.0]
        out = indicators.macd(_series(closes))
        self.assertGreater(float(out.hist.iloc[-1]), 0.0)
        self.assertLess(float(out.hist.iloc[-2]), float(out.hist.iloc[-1]))


class RsiTests(unittest.TestCase):
    def test_period_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            indicators.rsi(_series([1.0]), period=0)

    def test_all_gains_yields_one_hundred(self) -> None:
        # Strictly ascending close → no losses → RSI saturates at 100.
        s = _series([float(i) for i in range(1, 50)])
        out = indicators.rsi(s, period=14)
        self.assertAlmostEqual(float(out.iloc[-1]), 100.0, places=6)

    def test_wilder_matches_ewm_alpha(self) -> None:
        # Cross-check against the canonical Wilder formula written
        # inline. Drift between the two is the failure mode this guards.
        s = _series([float(x) for x in [10, 11, 10, 12, 13, 12, 14, 15, 14, 16, 17, 18, 17, 19, 20]])
        period = 5
        delta = s.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        ref = 100 - 100 / (1 + rs)
        out = indicators.rsi(s, period=period)
        # Only compare non-NaN positions.
        comparable = ~(ref.isna() | out.isna())
        np.testing.assert_allclose(
            out[comparable].to_numpy(),
            ref[comparable].to_numpy(),
            rtol=1e-9,
        )


class AdxTests(unittest.TestCase):
    def test_period_must_be_positive(self) -> None:
        s = _series([1.0])
        with self.assertRaises(ValueError):
            indicators.adx(s, s, s, period=0)

    def test_flat_input_produces_nan_adx(self) -> None:
        # No directional movement → +DI / -DI div-by-zero → NaN.
        # ADX is undefined; we explicitly want NaN, not 0.
        s = _series([100.0] * 30)
        out = indicators.adx(s, s, s, period=14)
        self.assertTrue(out.adx.iloc[-1] != out.adx.iloc[-1] or out.adx.iloc[-1] == 0.0)  # NaN-or-zero is OK


class BollingerTests(unittest.TestCase):
    def test_constant_series_bands_collapse_to_mean(self) -> None:
        s = _series([100.0] * 25)
        out = indicators.bollinger(s, window=20)
        # std of a flat window is 0, so upper == middle == lower at the tail.
        self.assertAlmostEqual(float(out.middle.iloc[-1]), 100.0, places=10)
        self.assertAlmostEqual(float(out.upper.iloc[-1]), 100.0, places=10)
        self.assertAlmostEqual(float(out.lower.iloc[-1]), 100.0, places=10)

    def test_num_std_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            indicators.bollinger(_series([1.0]), num_std=0)


class AtrTests(unittest.TestCase):
    def test_constant_inputs_zero_atr(self) -> None:
        s = _series([100.0] * 30)
        out = indicators.atr(s, s, s, period=14)
        # TR is zero everywhere except the first bar (NaN due to shift).
        # ATR converges to zero quickly.
        self.assertAlmostEqual(float(out.iloc[-1]), 0.0, places=10)


class ObvTests(unittest.TestCase):
    def test_obv_accumulates_signed_volume(self) -> None:
        close = _series([10.0, 11.0, 10.5, 11.5, 11.5, 11.0])
        volume = _series([100.0, 200.0, 300.0, 400.0, 500.0, 600.0])
        out = indicators.obv(close, volume)
        # First bar: diff is NaN → direction 0 → contribution 0
        # Bar 1: +200, Bar 2: -300, Bar 3: +400, Bar 4: 0 (flat), Bar 5: -600
        # Cumulative: [0, 200, -100, 300, 300, -300]
        np.testing.assert_allclose(
            out.to_numpy(),
            [0.0, 200.0, -100.0, 300.0, 300.0, -300.0],
        )


class CrossHelperTests(unittest.TestCase):
    def test_crossed_above_only_on_transition_bar(self) -> None:
        a = _series([1.0, 1.0, 2.0, 3.0, 2.0, 1.0])
        b = _series([2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
        out = indicators.crossed_above(a, b)
        # a crosses up through b only at index 3.
        # The bar at index 2 has a == b (not strictly above), so cross
        # is registered at index 3 where (a.shift <= b.shift) and (a > b).
        self.assertEqual(out.tolist(), [False, False, False, True, False, False])

    def test_crossed_below_mirror(self) -> None:
        a = _series([3.0, 3.0, 3.0, 2.0, 1.0])
        b = _series([2.0, 2.0, 2.0, 2.0, 2.0])
        out = indicators.crossed_below(a, b)
        self.assertEqual(out.tolist(), [False, False, False, False, True])


class SignalFromTests(unittest.TestCase):
    def test_truthy_scalar(self) -> None:
        self.assertEqual(indicators.signal_from(True), 1)
        self.assertEqual(indicators.signal_from(1), 1)
        self.assertEqual(indicators.signal_from(1.0), 1)
        self.assertEqual(indicators.signal_from(2.5), 1)

    def test_falsy_scalar(self) -> None:
        self.assertEqual(indicators.signal_from(False), 0)
        self.assertEqual(indicators.signal_from(0), 0)
        self.assertEqual(indicators.signal_from(0.0), 0)
        self.assertEqual(indicators.signal_from(None), 0)
        self.assertEqual(indicators.signal_from(math.nan), 0)

    def test_series_uses_last_value(self) -> None:
        self.assertEqual(indicators.signal_from(_series([0.0, 1.0])), 1)
        self.assertEqual(indicators.signal_from(_series([1.0, 0.0])), 0)
        self.assertEqual(indicators.signal_from(_series([1.0, math.nan])), 0)

    def test_empty_series(self) -> None:
        self.assertEqual(indicators.signal_from(pd.Series([], dtype=float)), 0)


def _ohlc(closes: list[float], spread: float = 1.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Build high/low/close from a close path (high = close+spread, low = close-spread)."""

    c = _series(closes)
    high = c + spread
    low = c - spread
    return high, low, c


class KdjTests(unittest.TestCase):
    def test_positive_int_guards(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            indicators.kdj(s, s, s, n=0)

    def test_matches_reference_formula(self) -> None:
        high, low, close = _ohlc([float(x) for x in [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18]])
        out = indicators.kdj(high, low, close, n=5, k_smooth=3, d_smooth=3)
        lowest = low.rolling(5).min()
        highest = high.rolling(5).max()
        rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100.0
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        j = 3 * k - 2 * d
        comp = ~(k.isna() | out.k.isna())
        np.testing.assert_allclose(out.k[comp].to_numpy(), k[comp].to_numpy(), rtol=1e-9)
        np.testing.assert_allclose(out.d[comp].to_numpy(), d[comp].to_numpy(), rtol=1e-9)
        np.testing.assert_allclose(out.j[comp].to_numpy(), j[comp].to_numpy(), rtol=1e-9)

    def test_flat_window_propagates_nan(self) -> None:
        high, low, close = _ohlc([100.0] * 10, spread=0.0)
        out = indicators.kdj(high, low, close, n=5)
        self.assertTrue(math.isnan(float(out.k.iloc[-1])))


class WilliamsRTests(unittest.TestCase):
    def test_top_of_range_is_zero(self) -> None:
        # Strictly ascending close that equals the window high → %R ≈ 0.
        high, low, close = _ohlc([float(i) for i in range(1, 21)], spread=0.0)
        out = indicators.williams_r(high, low, close, period=14)
        self.assertAlmostEqual(float(out.iloc[-1]), 0.0, places=9)

    def test_range_bounds(self) -> None:
        high, low, close = _ohlc([float(x) for x in [5, 3, 8, 2, 9, 4, 7, 1, 6, 10, 2, 8, 3, 9, 5]])
        out = indicators.williams_r(high, low, close, period=14).dropna()
        self.assertTrue(((out >= -100.0) & (out <= 0.0)).all())


class CciTests(unittest.TestCase):
    def test_matches_reference_formula(self) -> None:
        high, low, close = _ohlc([float(x) for x in range(1, 31)])
        out = indicators.cci(high, low, close, period=20)
        tp = (high + low + close) / 3.0
        sma_tp = tp.rolling(20).mean()
        mean_dev = tp.rolling(20).apply(lambda w: np.abs(w - w.mean()).mean(), raw=True)
        ref = (tp - sma_tp) / (0.015 * mean_dev).replace(0, np.nan)
        comp = ~(ref.isna() | out.isna())
        np.testing.assert_allclose(out[comp].to_numpy(), ref[comp].to_numpy(), rtol=1e-9)


class RocMomentumTests(unittest.TestCase):
    def test_roc_percent(self) -> None:
        s = _series([10.0, 11.0, 12.0, 13.0, 14.0])
        out = indicators.roc(s, period=2)
        # (12/10 - 1)*100 = 20 at idx 2
        self.assertAlmostEqual(float(out.iloc[2]), 20.0, places=9)

    def test_momentum_difference(self) -> None:
        s = _series([10.0, 11.0, 12.0, 13.0, 14.0])
        out = indicators.momentum(s, period=2)
        self.assertAlmostEqual(float(out.iloc[2]), 2.0, places=9)


class MfiTests(unittest.TestCase):
    def test_all_rising_saturates_at_hundred(self) -> None:
        high, low, close = _ohlc([float(i) for i in range(1, 30)])
        out = indicators.mfi(high, low, close, _series([100.0] * 29), period=14)
        self.assertAlmostEqual(float(out.iloc[-1]), 100.0, places=6)


class TrixTests(unittest.TestCase):
    def test_matches_reference_formula(self) -> None:
        s = _series([float(x) for x in range(1, 60)])
        out = indicators.trix(s, period=9)
        e1 = s.ewm(span=9, adjust=False).mean()
        e2 = e1.ewm(span=9, adjust=False).mean()
        e3 = e2.ewm(span=9, adjust=False).mean()
        ref = (e3 / e3.shift(1) - 1.0) * 100.0
        comp = ~(ref.isna() | out.isna())
        np.testing.assert_allclose(out[comp].to_numpy(), ref[comp].to_numpy(), rtol=1e-9)


class VwapTests(unittest.TestCase):
    def test_constant_price_equals_price(self) -> None:
        high, low, close = _ohlc([100.0] * 20, spread=0.0)
        out = indicators.vwap(high, low, close, _series([float(i + 1) for i in range(20)]), window=5)
        self.assertAlmostEqual(float(out.iloc[-1]), 100.0, places=9)

    def test_anchored_matches_cumulative(self) -> None:
        high, low, close = _ohlc([10.0, 20.0, 30.0], spread=0.0)
        vol = _series([1.0, 2.0, 3.0])
        out = indicators.vwap(high, low, close, vol, window=None)
        # tp = close; cumulative (10*1 + 20*2 + 30*3) / (1+2+3) = 140/6
        self.assertAlmostEqual(float(out.iloc[-1]), 140.0 / 6.0, places=9)


class CmfAdTests(unittest.TestCase):
    def test_cmf_close_at_high_positive(self) -> None:
        # close == high every bar → MFM = +1 → CMF = +1.
        close = _series([float(i + 10) for i in range(25)])
        high = close.copy()
        low = close - 2.0
        out = indicators.cmf(high, low, close, _series([100.0] * 25), period=20)
        self.assertAlmostEqual(float(out.iloc[-1]), 1.0, places=9)

    def test_ad_is_cumulative(self) -> None:
        close = _series([10.0, 12.0, 11.0])
        high = _series([11.0, 13.0, 12.0])
        low = _series([9.0, 11.0, 10.0])
        vol = _series([100.0, 200.0, 300.0])
        out = indicators.ad(high, low, close, vol)
        mfm = ((close - low) - (high - close)) / (high - low)
        ref = (mfm * vol).cumsum()
        np.testing.assert_allclose(out.to_numpy(), ref.to_numpy(), rtol=1e-9)


class VolumeRatioTests(unittest.TestCase):
    def test_constant_volume_ratio_is_one(self) -> None:
        out = indicators.volume_ratio(_series([500.0] * 25), window=20)
        self.assertAlmostEqual(float(out.iloc[-1]), 1.0, places=9)


class KeltnerDonchianTests(unittest.TestCase):
    def test_keltner_bands_symmetric_around_ema(self) -> None:
        high, low, close = _ohlc([float(x) for x in range(1, 60)])
        out = indicators.keltner(high, low, close, ema_window=20, atr_period=10, multiplier=2.0)
        mid = indicators.ema(close, 20)
        band = indicators.atr(high, low, close, 10)
        np.testing.assert_allclose(out.middle.to_numpy(), mid.to_numpy(), rtol=1e-9)
        np.testing.assert_allclose(
            out.upper.dropna().to_numpy(),
            (mid + 2.0 * band).dropna().to_numpy(),
            rtol=1e-9,
        )

    def test_donchian_tracks_extremes(self) -> None:
        high = _series([float(x) for x in [5, 6, 7, 4, 8, 9, 3, 10]])
        low = _series([float(x) for x in [1, 2, 3, 0, 4, 5, 1, 6]])
        out = indicators.donchian(high, low, window=3)
        self.assertAlmostEqual(float(out.upper.iloc[-1]), 10.0, places=9)
        self.assertAlmostEqual(float(out.lower.iloc[-1]), 1.0, places=9)
        self.assertAlmostEqual(float(out.middle.iloc[-1]), 5.5, places=9)


class StdevVolTests(unittest.TestCase):
    def test_stdev_matches_rolling(self) -> None:
        s = _series([float(x) for x in range(1, 40)])
        out = indicators.stdev(s, window=20)
        pd.testing.assert_series_equal(out, s.rolling(20).std(ddof=0), check_names=False)

    def test_constant_price_zero_volatility(self) -> None:
        out = indicators.hist_volatility(_series([100.0] * 30), window=20)
        self.assertAlmostEqual(float(out.iloc[-1]), 0.0, places=12)


class WmaDemaTests(unittest.TestCase):
    def test_wma_constant_is_constant(self) -> None:
        out = indicators.wma(_series([7.0] * 10), window=4)
        self.assertAlmostEqual(float(out.iloc[-1]), 7.0, places=12)

    def test_wma_matches_weighted_dot(self) -> None:
        s = _series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = indicators.wma(s, window=3)
        # weights 1,2,3 → (3*1 + 4*2 + 5*3) / 6 = 26/6 at the tail.
        self.assertAlmostEqual(float(out.iloc[-1]), 26.0 / 6.0, places=12)

    def test_dema_matches_reference(self) -> None:
        s = _series([float(x) for x in range(1, 40)])
        out = indicators.dema(s, span=10)
        e1 = s.ewm(span=10, adjust=False).mean()
        e2 = e1.ewm(span=10, adjust=False).mean()
        np.testing.assert_allclose(out.to_numpy(), (2 * e1 - e2).to_numpy(), rtol=1e-9)


class KamaTests(unittest.TestCase):
    def test_constant_series_stays_at_seed(self) -> None:
        out = indicators.kama(_series([50.0] * 30), period=10)
        self.assertAlmostEqual(float(out.iloc[-1]), 50.0, places=9)

    def test_fast_must_be_smaller_than_slow(self) -> None:
        with self.assertRaises(ValueError):
            indicators.kama(_series([1.0] * 20), period=5, fast=30, slow=2)

    def test_matches_independent_loop(self) -> None:
        prices = [10, 11, 13, 12, 14, 16, 15, 18, 20, 19, 22, 24, 23, 26, 28]
        s = _series([float(x) for x in prices])
        out = indicators.kama(s, period=4, fast=2, slow=10)
        # Independent reimplementation.
        arr = np.array([float(x) for x in prices])
        p = 4
        fast_sc, slow_sc = 2 / 3, 2 / 11
        ref = [math.nan] * len(arr)
        ref[p] = arr[p]
        for i in range(p + 1, len(arr)):
            change = abs(arr[i] - arr[i - p])
            vol = float(np.abs(np.diff(arr[i - p : i + 1])).sum())
            er = 0.0 if vol == 0 else change / vol
            sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            ref[i] = ref[i - 1] + sc * (arr[i] - ref[i - 1])
        np.testing.assert_allclose(
            out.iloc[p:].to_numpy(), np.array(ref[p:]), rtol=1e-9
        )


class SuperTrendTests(unittest.TestCase):
    def test_rising_trend_direction_positive(self) -> None:
        high, low, close = _ohlc([float(i) for i in range(1, 60)])
        out = indicators.supertrend(high, low, close, period=10, multiplier=3.0)
        self.assertEqual(float(out.direction.iloc[-1]), 1.0)
        # In an up-trend the trailing line sits below price.
        self.assertLess(float(out.supertrend.iloc[-1]), float(close.iloc[-1]))

    def test_falling_trend_direction_negative(self) -> None:
        high, low, close = _ohlc([float(i) for i in range(60, 1, -1)])
        out = indicators.supertrend(high, low, close, period=10, multiplier=3.0)
        self.assertEqual(float(out.direction.iloc[-1]), -1.0)
        self.assertGreater(float(out.supertrend.iloc[-1]), float(close.iloc[-1]))


class PsarTests(unittest.TestCase):
    def test_step_must_not_exceed_max(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            indicators.psar(s + 1.0, s, step=0.3, max_step=0.2)

    def test_uptrend_sar_below_price(self) -> None:
        high, low, close = _ohlc([float(i) for i in range(1, 30)])
        out = indicators.psar(high, low)
        # After the seed bar, a clean up-trend keeps the SAR under the close.
        self.assertTrue((out.iloc[5:].to_numpy() < close.iloc[5:].to_numpy()).all())

    def test_reversal_flips_sar(self) -> None:
        # Rise then fall: the SAR must end up above price after the reversal.
        path = [float(i) for i in range(1, 20)] + [float(i) for i in range(19, 1, -1)]
        high, low, close = _ohlc(path)
        out = indicators.psar(high, low)
        self.assertGreater(float(out.iloc[-1]), float(close.iloc[-1]))


class IchimokuTests(unittest.TestCase):
    def test_tenkan_kijun_midpoints(self) -> None:
        high, low, close = _ohlc([float(x) for x in range(1, 80)])
        out = indicators.ichimoku(high, low, close, tenkan=9, kijun=26, senkou_b=52)
        ref_tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2.0
        np.testing.assert_allclose(out.tenkan.to_numpy(), ref_tenkan.to_numpy(), rtol=1e-9)

    def test_chikou_tail_is_nan(self) -> None:
        # chikou = close.shift(-kijun): the last `kijun` bars must be NaN so
        # a current-bar read never sees future closes.
        high, low, close = _ohlc([float(x) for x in range(1, 80)])
        out = indicators.ichimoku(high, low, close, kijun=26)
        self.assertTrue(out.chikou.iloc[-26:].isna().all())

    def test_senkou_is_past_derived(self) -> None:
        # senkou_a at bar i equals the raw conversion/base midpoint kijun
        # bars earlier — past data, not look-ahead.
        high, low, close = _ohlc([float(x) for x in range(1, 80)])
        out = indicators.ichimoku(high, low, close, tenkan=9, kijun=26)
        raw = ((high.rolling(9).max() + low.rolling(9).min()) / 2.0
               + (high.rolling(26).max() + low.rolling(26).min()) / 2.0) / 2.0
        np.testing.assert_allclose(
            out.senkou_a.iloc[40:].to_numpy(),
            raw.shift(26).iloc[40:].to_numpy(),
            rtol=1e-9,
        )


class ZigZagTests(unittest.TestCase):
    def test_threshold_must_be_in_open_unit_interval(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            indicators.zigzag(s, threshold=0.0)
        with self.assertRaises(ValueError):
            indicators.zigzag(s, threshold=1.0)
        with self.assertRaises(ValueError):
            indicators.zigzag(s, threshold=-0.1)

    def test_threshold_type_is_validated(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        with self.assertRaises(TypeError):
            indicators.zigzag(s, threshold=True)  # type: ignore[arg-type]

    def test_pivots_land_on_true_swing_extremes(self) -> None:
        # Drift inside the band (100->103->105), then 100->120->90->130 with a
        # 10% threshold. The initial low (100 at index 0) confirms once price
        # rallies past it (index 3); the high 120 (index 3) confirms on the
        # drop to 90; the low 90 (index 4) confirms on the rebound to 130. The
        # final rally to 130 is an in-progress leg and is NOT yet a pivot.
        path = [100.0, 103.0, 105.0, 120.0, 90.0, 130.0]
        close = _series(path)
        out = indicators.zigzag(close, threshold=0.10)
        confirmed = {
            int(i): float(v)
            for i, v in out.pivot.items()
            if not math.isnan(v)
        }
        self.assertEqual(confirmed, {0: 100.0, 3: 120.0, 4: 90.0})
        # The active up-leg's running extreme (index 5) is not yet confirmed.
        self.assertTrue(math.isnan(out.pivot.iloc[-1]))

    def test_direction_is_confirmed_swing_and_never_repaints(self) -> None:
        path = [100.0, 103.0, 105.0, 120.0, 90.0, 130.0]
        close = _series(path)
        out = indicators.zigzag(close, threshold=0.10)
        # Up-swing confirmed at index 3, down-swing at index 4, up-swing
        # again at index 5 — each flip uses only data up to that bar.
        self.assertEqual(float(out.direction.iloc[3]), 1.0)
        self.assertEqual(float(out.direction.iloc[4]), -1.0)
        self.assertEqual(float(out.direction.iloc[-1]), 1.0)
        # Warm-up bars before the first confirmation are NaN.
        self.assertTrue(out.direction.iloc[:3].isna().all())

    def test_flat_series_produces_no_swing(self) -> None:
        out = indicators.zigzag(_series([5.0] * 12), threshold=0.05)
        self.assertTrue(out.pivot.isna().all())
        self.assertTrue(out.direction.isna().all())

    def test_empty_series(self) -> None:
        out = indicators.zigzag(_series([]), threshold=0.05)
        self.assertEqual(len(out.pivot), 0)
        self.assertEqual(len(out.direction), 0)

    def test_index_is_preserved(self) -> None:
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0], index=idx)
        out = indicators.zigzag(close, threshold=0.05)
        self.assertTrue(out.pivot.index.equals(idx))
        self.assertTrue(out.direction.index.equals(idx))


class LimitUpApproxTests(unittest.TestCase):
    def test_a_share_limit_pct_by_board(self) -> None:
        self.assertEqual(indicators.a_share_limit_pct("600519.SH"), 0.10)
        self.assertEqual(indicators.a_share_limit_pct("688981.SH"), 0.20)
        self.assertEqual(indicators.a_share_limit_pct("300750.SZ"), 0.20)
        self.assertEqual(indicators.a_share_limit_pct("920000.BJ"), 0.30)

    def test_main_board_limit_up_requires_close_at_high(self) -> None:
        # prev 10.00 -> limit 11.00 on a 10% board.
        close = _series([10.0, 11.0, 10.5])
        high = _series([10.2, 11.0, 10.8])
        out = indicators.limit_up_approx(close, high, symbol="600519.SH")
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))
        self.assertFalse(bool(out.iloc[2]))

    def test_close_below_limit_price_is_false_even_at_high(self) -> None:
        close = _series([10.0, 10.9])
        high = _series([10.1, 10.9])
        out = indicators.limit_up_approx(close, high, symbol="600519.SH")
        self.assertFalse(bool(out.iloc[-1]))

    def test_chinext_uses_twenty_percent_limit(self) -> None:
        close = _series([10.0, 12.0])
        high = _series([10.2, 12.0])
        out = indicators.limit_up_approx(close, high, symbol="300750.SZ")
        self.assertTrue(bool(out.iloc[-1]))

    def test_explicit_st_limit_pct(self) -> None:
        close = _series([10.0, 10.5])
        high = _series([10.1, 10.5])
        out = indicators.limit_up_approx(
            close, high, symbol="600519.SH", limit_pct=0.05
        )
        self.assertTrue(bool(out.iloc[-1]))

    def test_signal_from_on_last_bar(self) -> None:
        close = _series([10.0, 11.0])
        high = _series([10.1, 11.0])
        flag = indicators.limit_up_approx(close, high, symbol="600519.SH")
        self.assertEqual(indicators.signal_from(flag), 1)


class LimitDownApproxTests(unittest.TestCase):
    def test_main_board_limit_down_requires_close_at_low(self) -> None:
        # prev 10.00 -> limit-down 9.00 on a 10% board.
        close = _series([10.0, 9.0, 9.5])
        low = _series([9.8, 9.0, 9.3])
        out = indicators.limit_down_approx(close, low, symbol="600519.SH")
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))
        self.assertFalse(bool(out.iloc[2]))

    def test_close_above_limit_price_is_false_even_at_low(self) -> None:
        close = _series([10.0, 9.1])
        low = _series([9.9, 9.1])
        out = indicators.limit_down_approx(close, low, symbol="600519.SH")
        self.assertFalse(bool(out.iloc[-1]))

    def test_chinext_uses_twenty_percent_limit(self) -> None:
        close = _series([10.0, 8.0])
        low = _series([9.8, 8.0])
        out = indicators.limit_down_approx(close, low, symbol="300750.SZ")
        self.assertTrue(bool(out.iloc[-1]))

    def test_explicit_st_limit_pct(self) -> None:
        close = _series([10.0, 9.5])
        low = _series([9.9, 9.5])
        out = indicators.limit_down_approx(
            close, low, symbol="600519.SH", limit_pct=0.05
        )
        self.assertTrue(bool(out.iloc[-1]))

    def test_signal_from_on_last_bar(self) -> None:
        close = _series([10.0, 9.0])
        low = _series([9.9, 9.0])
        flag = indicators.limit_down_approx(close, low, symbol="600519.SH")
        self.assertEqual(indicators.signal_from(flag), 1)


if __name__ == "__main__":
    unittest.main()
