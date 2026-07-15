"""Tests for ``doyoutrade.strategy_sdk.patterns``.

The most important class of tests here is :class:`TestCausality`: for every
public function in ``patterns.__all__`` we assert that the value at any bar
``i`` depends only on bars ``<= i``. We do that by running the function on
a "short" prefix of an OHLCV series and on a "long" version of the same
series (the short prefix plus extra future bars) and comparing the head
slices. Any silent lookahead — ``.shift(-N)``, ``center=True`` consumed at
``i``, "stamp pivot at pivot bar" instead of "stamp at confirmation bar" —
would surface as the two head slices disagreeing.

We deliberately write **one test method per function** rather than looping
inside one method so that a regression in (say) ``swing_high`` doesn't
hide a separate regression in ``head_and_shoulders`` behind the first
failure.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from doyoutrade.strategy_sdk import patterns
from doyoutrade.strategy_runtime.compiler import StrategyCompiler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ohlcv(n: int, seed: int = 0) -> pd.DataFrame:
    """Build a deterministic OHLCV DataFrame with random-walk closes."""

    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = close + rng.normal(0.0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


# Confirmation prefix length we compare across short / long runs. Must be
# large enough that all warm-ups (rolling window, swing left+right) are
# past so the head slice has many real data points to compare.
_PREFIX_K = 60
_FUTURE_PADDING = 15


def _assert_causal_series(
    tc: unittest.TestCase, short: pd.Series, long_: pd.Series
) -> None:
    """Compare the first ``_PREFIX_K`` values; NaN == NaN counts as equal."""

    pd.testing.assert_series_equal(
        short.iloc[:_PREFIX_K].reset_index(drop=True),
        long_.iloc[:_PREFIX_K].reset_index(drop=True),
        check_names=False,
    )


# ---------------------------------------------------------------------------
# 1. Causality / lookahead tests — one method per public function.
# ---------------------------------------------------------------------------


class TestCausality(unittest.TestCase):
    """Each public ``patterns.*`` function must be lookahead-safe.

    For every function ``f``, we compute ``f(df_full)`` and
    ``f(df_short)`` where ``df_short = df_full.iloc[:K + _FUTURE_PADDING]``
    and ``df_full`` has even more bars. The first ``K`` rows of both runs
    must match exactly. Any function that peeks at index ``j > i`` to
    produce its output at ``i`` will fail this test because the two runs
    have different "futures".
    """

    def setUp(self) -> None:
        # Use a longer series for `_full`; the `short` slice has only
        # _PREFIX_K + _FUTURE_PADDING bars so the long series genuinely
        # has additional data the short series cannot see.
        self.full = _ohlcv(200, seed=42)
        self.short = self.full.iloc[: _PREFIX_K + _FUTURE_PADDING].copy()

    # --- Candlestick ----------------------------------------------------

    def test_is_doji_is_causal(self) -> None:
        f = patterns.is_doji
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_hammer_is_causal(self) -> None:
        f = patterns.is_hammer
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_inverted_hammer_is_causal(self) -> None:
        f = patterns.is_inverted_hammer
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_bullish_engulfing_is_causal(self) -> None:
        f = patterns.is_bullish_engulfing
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_bearish_engulfing_is_causal(self) -> None:
        f = patterns.is_bearish_engulfing
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_bullish_harami_is_causal(self) -> None:
        f = patterns.is_bullish_harami
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    def test_is_bearish_harami_is_causal(self) -> None:
        f = patterns.is_bearish_harami
        _assert_causal_series(
            self,
            f(self.short["open"], self.short["high"], self.short["low"], self.short["close"]),
            f(self.full["open"], self.full["high"], self.full["low"], self.full["close"]),
        )

    # --- Price levels / breakouts / bounces -----------------------------

    def test_prior_high_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.prior_high(self.short["high"], 20),
            patterns.prior_high(self.full["high"], 20),
        )

    def test_prior_low_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.prior_low(self.short["low"], 20),
            patterns.prior_low(self.full["low"], 20),
        )

    def test_broke_above_is_causal(self) -> None:
        # Use a Series level computed from `high` itself so any future
        # leakage in `level`'s construction would surface too.
        short_level = patterns.prior_high(self.short["high"], 20)
        full_level = patterns.prior_high(self.full["high"], 20)
        _assert_causal_series(
            self,
            patterns.broke_above(self.short["close"], short_level),
            patterns.broke_above(self.full["close"], full_level),
        )

    def test_broke_below_is_causal(self) -> None:
        short_level = patterns.prior_low(self.short["low"], 20)
        full_level = patterns.prior_low(self.full["low"], 20)
        _assert_causal_series(
            self,
            patterns.broke_below(self.short["close"], short_level),
            patterns.broke_below(self.full["close"], full_level),
        )

    def test_touched_above_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.touched_above(self.short["high"], 105.0),
            patterns.touched_above(self.full["high"], 105.0),
        )

    def test_touched_below_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.touched_below(self.short["low"], 95.0),
            patterns.touched_below(self.full["low"], 95.0),
        )

    def test_bounced_from_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.bounced_from(self.short["low"], self.short["close"], 95.0, tol=0.02),
            patterns.bounced_from(self.full["low"], self.full["close"], 95.0, tol=0.02),
        )

    # --- Swings ---------------------------------------------------------

    def test_swing_high_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.swing_high(self.short["high"], left=3, right=3),
            patterns.swing_high(self.full["high"], left=3, right=3),
        )

    def test_swing_low_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.swing_low(self.short["low"], left=3, right=3),
            patterns.swing_low(self.full["low"], left=3, right=3),
        )

    def test_last_swing_high_level_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.last_swing_high_level(self.short["high"], left=3, right=3),
            patterns.last_swing_high_level(self.full["high"], left=3, right=3),
        )

    def test_last_swing_low_level_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.last_swing_low_level(self.short["low"], left=3, right=3),
            patterns.last_swing_low_level(self.full["low"], left=3, right=3),
        )

    # --- Structural patterns -------------------------------------------

    def test_double_top_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.double_top(self.short["high"], left=3, right=3, tol=0.03),
            patterns.double_top(self.full["high"], left=3, right=3, tol=0.03),
        )

    def test_double_bottom_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.double_bottom(self.short["low"], left=3, right=3, tol=0.03),
            patterns.double_bottom(self.full["low"], left=3, right=3, tol=0.03),
        )

    def test_head_and_shoulders_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.head_and_shoulders(self.short["high"], left=3, right=3, shoulder_tol=0.05),
            patterns.head_and_shoulders(self.full["high"], left=3, right=3, shoulder_tol=0.05),
        )

    def test_triangle_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.triangle(self.short["high"], self.short["low"], window=20, left=3, right=3),
            patterns.triangle(self.full["high"], self.full["low"], window=20, left=3, right=3),
        )

    def test_broadening_is_causal(self) -> None:
        _assert_causal_series(
            self,
            patterns.broadening(self.short["high"], self.short["low"], window=20, left=3, right=3),
            patterns.broadening(self.full["high"], self.full["low"], window=20, left=3, right=3),
        )

    def test_all_public_functions_covered(self) -> None:
        """Guard rail: if patterns gains a new public function, force a new
        causality test method here. Failing this test = author of the new
        function must add a `_is_causal` method above.
        """

        tested = {
            "is_doji",
            "is_hammer",
            "is_inverted_hammer",
            "is_bullish_engulfing",
            "is_bearish_engulfing",
            "is_bullish_harami",
            "is_bearish_harami",
            "prior_high",
            "prior_low",
            "broke_above",
            "broke_below",
            "touched_above",
            "touched_below",
            "bounced_from",
            "swing_high",
            "swing_low",
            "last_swing_high_level",
            "last_swing_low_level",
            "double_top",
            "double_bottom",
            "head_and_shoulders",
            "triangle",
            "broadening",
        }
        self.assertEqual(set(patterns.__all__), tested)


# ---------------------------------------------------------------------------
# 2. Fixture correctness — known shapes should be detected.
# ---------------------------------------------------------------------------


def _candle_series(rows: list[dict[str, float]]) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Build (open, high, low, close) Series from a list of bar dicts."""

    df = pd.DataFrame(rows)
    return df["open"], df["high"], df["low"], df["close"]


class TestCandlestickFixtures(unittest.TestCase):
    """Hand-constructed bars that must trigger each candlestick pattern."""

    def test_is_doji_detects_small_body(self) -> None:
        # Two normal bars then a tight-body bar (body 0.01 vs range 4).
        open_, high, low, close = _candle_series(
            [
                {"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.8},
                {"open": 10.8, "high": 11.5, "low": 10.5, "close": 11.2},
                {"open": 12.00, "high": 14.0, "low": 10.0, "close": 12.01},
            ]
        )
        out = patterns.is_doji(open_, high, low, close)
        self.assertTrue(bool(out.iloc[2]))
        self.assertFalse(bool(out.iloc[0]))
        self.assertFalse(bool(out.iloc[1]))

    def test_is_hammer_detects_long_lower_shadow(self) -> None:
        # Bar with body=0.5 (10.0 -> 10.5), lower shadow = 10.0 - 7.0 = 3.0
        # (> 2 * body), upper shadow = 10.7 - 10.5 = 0.2 (< body). Not doji.
        open_, high, low, close = _candle_series(
            [
                {"open": 10.0, "high": 10.7, "low": 7.0, "close": 10.5},
            ]
        )
        out = patterns.is_hammer(open_, high, low, close)
        self.assertTrue(bool(out.iloc[0]))

    def test_is_inverted_hammer_detects_long_upper_shadow(self) -> None:
        # body 0.5, upper shadow 3.0, lower shadow 0.2.
        open_, high, low, close = _candle_series(
            [
                {"open": 10.5, "high": 14.0, "low": 10.3, "close": 11.0},
            ]
        )
        out = patterns.is_inverted_hammer(open_, high, low, close)
        self.assertTrue(bool(out.iloc[0]))

    def test_is_bullish_engulfing_detects(self) -> None:
        # Bar 0: bearish (open 12 -> close 10, body 2).
        # Bar 1: bullish, open 9.5 (<= prev close 10), close 12.5 (>= prev open 12),
        #        body 3 (> prev body 2).
        open_, high, low, close = _candle_series(
            [
                {"open": 12.0, "high": 12.5, "low": 9.5, "close": 10.0},
                {"open": 9.5, "high": 13.0, "low": 9.3, "close": 12.5},
            ]
        )
        out = patterns.is_bullish_engulfing(open_, high, low, close)
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))

    def test_is_bearish_engulfing_detects(self) -> None:
        # Bar 0: bullish (open 10 -> close 12, body 2).
        # Bar 1: bearish, open 12.5 (>= prev close 12), close 9.5 (<= prev open 10),
        #        body 3 (> prev body 2).
        open_, high, low, close = _candle_series(
            [
                {"open": 10.0, "high": 12.5, "low": 9.8, "close": 12.0},
                {"open": 12.5, "high": 12.7, "low": 9.0, "close": 9.5},
            ]
        )
        out = patterns.is_bearish_engulfing(open_, high, low, close)
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))

    def test_is_bullish_harami_detects(self) -> None:
        # Bar 0: large bearish — open 12, close 8 (body 4, range 4, body/range 1.0 > 0.5).
        # Bar 1: small bullish inside prev body — open 9 (>= prev close 8),
        #        close 11 (<= prev open 12), body 2 (< prev body 4).
        open_, high, low, close = _candle_series(
            [
                {"open": 12.0, "high": 12.0, "low": 8.0, "close": 8.0},
                {"open": 9.0, "high": 11.5, "low": 8.5, "close": 11.0},
            ]
        )
        out = patterns.is_bullish_harami(open_, high, low, close)
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))

    def test_is_bearish_harami_detects(self) -> None:
        # Bar 0: large bullish — open 8, close 12 (body 4, range 4, ratio 1.0).
        # Bar 1: small bearish inside prev body — open 11 (<= prev close 12),
        #        close 9 (>= prev open 8), body 2 (< prev body 4).
        open_, high, low, close = _candle_series(
            [
                {"open": 8.0, "high": 12.0, "low": 8.0, "close": 12.0},
                {"open": 11.0, "high": 11.5, "low": 8.5, "close": 9.0},
            ]
        )
        out = patterns.is_bearish_harami(open_, high, low, close)
        self.assertFalse(bool(out.iloc[0]))
        self.assertTrue(bool(out.iloc[1]))


class TestPriceLevelFixtures(unittest.TestCase):
    def test_prior_high_excludes_current_bar(self) -> None:
        high = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = patterns.prior_high(high, lookback=3)
        # First 3 outputs are NaN (rolling warm-up + .shift(1)).
        self.assertTrue(np.isnan(out.iloc[0]))
        self.assertTrue(np.isnan(out.iloc[1]))
        self.assertTrue(np.isnan(out.iloc[2]))
        # At index 3: max of high[0..2] = max(1,2,3) = 3.0 (NOT 4.0).
        self.assertAlmostEqual(float(out.iloc[3]), 3.0)
        # At index 4: max of high[1..3] = max(2,3,4) = 4.0 (NOT 5.0).
        self.assertAlmostEqual(float(out.iloc[4]), 4.0)

    def test_prior_low_excludes_current_bar(self) -> None:
        low = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
        out = patterns.prior_low(low, lookback=3)
        self.assertTrue(np.isnan(out.iloc[2]))
        self.assertAlmostEqual(float(out.iloc[3]), 3.0)
        self.assertAlmostEqual(float(out.iloc[4]), 2.0)

    def test_broke_above_scalar_level(self) -> None:
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        out = patterns.broke_above(close, 3.5)
        # Only at index 3: close[2]=3 (<= 3.5) and close[3]=4 (> 3.5).
        self.assertEqual(out.tolist(), [False, False, False, True, False, False])

    def test_broke_below_scalar_level(self) -> None:
        close = pd.Series([6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
        out = patterns.broke_below(close, 3.5)
        # close[2]=4 (>= 3.5), close[3]=3 (< 3.5).
        self.assertEqual(out.tolist(), [False, False, False, True, False, False])

    def test_touched_above_first_touch_only(self) -> None:
        high = pd.Series([1.0, 2.0, 3.0, 4.0, 4.5, 3.0])
        out = patterns.touched_above(high, 3.5)
        # First touch at index 3 (high goes from 3 to 4).
        self.assertEqual(out.tolist(), [False, False, False, True, False, False])

    def test_touched_below_first_touch_only(self) -> None:
        low = pd.Series([5.0, 4.0, 3.0, 2.0, 1.5, 3.0])
        out = patterns.touched_below(low, 2.5)
        # First touch at index 3 (low goes from 3 to 2).
        self.assertEqual(out.tolist(), [False, False, False, True, False, False])

    def test_bounced_from_detects_dip_and_close_above(self) -> None:
        # low dips to within tol of support (100), close closes above support.
        low = pd.Series([105.0, 104.0, 100.5, 103.0])
        close = pd.Series([106.0, 105.0, 102.0, 104.0])
        out = patterns.bounced_from(low, close, support=100.0, tol=0.01)
        # 100.5 <= 100*(1.01) = 101.0; close 102 > 100. Bounce at index 2.
        self.assertEqual(out.tolist(), [False, False, True, False])


class TestSwingFixtures(unittest.TestCase):
    def test_swing_high_confirmed_at_pivot_plus_right(self) -> None:
        # Single clean peak at idx=4. With left=2, right=2 the pivot is
        # the local max over high[2..6], i.e. the value 14.
        high = pd.Series(
            [10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 10.0],
        )
        out = patterns.swing_high(high, left=2, right=2)
        # NOT confirmed at the pivot bar itself (idx 4) — right=2 bars in
        # the future must arrive.
        self.assertFalse(bool(out.iloc[4]))
        self.assertFalse(bool(out.iloc[5]))
        # Confirmed at idx 4 + right = 6.
        self.assertTrue(bool(out.iloc[6]))
        # No other confirmations.
        self.assertEqual(out.sum(), 1)

    def test_swing_low_confirmed_at_pivot_plus_right(self) -> None:
        low = pd.Series([10.0, 9.0, 8.0, 7.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        out = patterns.swing_low(low, left=2, right=2)
        self.assertFalse(bool(out.iloc[4]))
        self.assertTrue(bool(out.iloc[6]))
        self.assertEqual(out.sum(), 1)

    def test_last_swing_high_level_is_pivot_price(self) -> None:
        high = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 11.0, 10.0])
        out = patterns.last_swing_high_level(high, left=2, right=2)
        # Before confirmation at idx 6, no pivot known → NaN.
        self.assertTrue(np.isnan(out.iloc[5]))
        # At idx 6 the level equals the pivot bar's high = 14.
        self.assertAlmostEqual(float(out.iloc[6]), 14.0)
        # Forward-fill into the tail.
        self.assertAlmostEqual(float(out.iloc[8]), 14.0)


class TestStructuralFixtures(unittest.TestCase):
    def test_double_top_detected_at_second_peak_confirmation(self) -> None:
        # Construct two clean peaks of near-equal height with a valley in
        # between. left=right=1 keeps the test tight.
        high = pd.Series(
            [10.0, 12.0, 10.0, 8.0, 10.0, 11.95, 10.0, 9.0],
        )
        # Peaks at idx 1 (value 12) and idx 5 (value 11.95). With
        # left=right=1 those are local maxima of a 3-bar window.
        out = patterns.double_top(high, left=1, right=1, tol=0.05)
        # |12 - 11.95| / 11.975 ≈ 0.0042 < 0.05 → double top confirmed at
        # the confirmation bar of the second peak = idx 5 + right(1) = 6.
        self.assertTrue(bool(out.iloc[6]))
        # Before idx 6: not yet confirmed.
        self.assertEqual(int(out.iloc[:6].sum()), 0)

    def test_double_bottom_detected_at_second_valley_confirmation(self) -> None:
        low = pd.Series([10.0, 8.0, 10.0, 12.0, 10.0, 8.05, 10.0, 11.0])
        out = patterns.double_bottom(low, left=1, right=1, tol=0.05)
        self.assertTrue(bool(out.iloc[6]))

    def test_head_and_shoulders_at_third_peak(self) -> None:
        # Three confirmed peaks: lv=10, hv=12, rv=10.1. lv & rv within 5%.
        high = pd.Series(
            [
                8.0,
                10.0,  # peak 1 (left shoulder), pivot idx 1
                8.0,
                7.0,
                12.0,  # peak 2 (head), pivot idx 4
                8.0,
                7.5,
                10.1,  # peak 3 (right shoulder), pivot idx 7
                8.0,
                7.0,
            ]
        )
        out = patterns.head_and_shoulders(high, left=1, right=1, shoulder_tol=0.05)
        # Confirmation at idx 7 + right(1) = 8.
        self.assertTrue(bool(out.iloc[8]))

    def test_triangle_ascending_marks_plus_one(self) -> None:
        # Ascending: rising valleys, ~flat peaks. Build a clean zigzag where
        # each cycle contributes one confirmed peak near 20 and one
        # confirmed valley climbing 10 → 12 → 14 → 16. left=right=2 makes
        # the pivots unambiguous local extrema of a 5-bar window.
        valleys = [10.0, 12.0, 14.0, 16.0]
        peaks = [20.0, 20.05, 20.02, 20.03]
        bars: list[tuple[float, float]] = []
        for v, p in zip(valleys, peaks):
            bars.append((v + 0.1, v))            # valley bar
            bars.append((v + 0.5, v + 0.2))      # transition up
            bars.append((p - 0.5, p - 1.0))      # transition up
            bars.append((p, p - 0.3))            # peak bar
            bars.append((p - 0.5, p - 1.0))      # transition down
            bars.append((v + 1.5, v + 0.5))      # transition down
        high = pd.Series([b[0] for b in bars])
        low = pd.Series([b[1] for b in bars])
        # window strictly < len so the rolling loop actually visits bars
        # that see all four peaks + valleys.
        out = patterns.triangle(high, low, window=20, left=2, right=2)
        self.assertGreaterEqual(int((out == 1).sum()), 1)
        # And no descending marks anywhere in this ascending fixture.
        self.assertEqual(int((out == -1).sum()), 0)


# ---------------------------------------------------------------------------
# 3. Input guards — must raise on bad input, not silently coerce.
# ---------------------------------------------------------------------------


class TestInputGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])

    # --- non-Series rejection ------------------------------------------

    def test_is_doji_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_doji([1.0], self.s, self.s, self.s)
        with self.assertRaises(TypeError):
            patterns.is_doji(self.s, np.array([1.0]), self.s, self.s)

    def test_is_hammer_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_hammer([1.0], self.s, self.s, self.s)

    def test_is_inverted_hammer_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_inverted_hammer(self.s.tolist(), self.s, self.s, self.s)

    def test_is_bullish_engulfing_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_bullish_engulfing(self.s, self.s, self.s, [1.0])

    def test_is_bearish_engulfing_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_bearish_engulfing(self.s, self.s, self.s, [1.0])

    def test_is_bullish_harami_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_bullish_harami(self.s, self.s, self.s, [1.0])

    def test_is_bearish_harami_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.is_bearish_harami(self.s, self.s, self.s, [1.0])

    def test_prior_high_rejects_non_series_and_bad_lookback(self) -> None:
        with self.assertRaises(TypeError):
            patterns.prior_high([1, 2, 3], 3)
        with self.assertRaises(ValueError):
            patterns.prior_high(self.s, 0)
        with self.assertRaises(ValueError):
            patterns.prior_high(self.s, -1)

    def test_prior_low_rejects_non_series_and_bad_lookback(self) -> None:
        with self.assertRaises(TypeError):
            patterns.prior_low(np.array([1.0]), 3)
        with self.assertRaises(ValueError):
            patterns.prior_low(self.s, 0)

    def test_broke_above_rejects_bad_level(self) -> None:
        with self.assertRaises(TypeError):
            patterns.broke_above(self.s, {"x": 1})
        with self.assertRaises(TypeError):
            patterns.broke_above(self.s, None)
        # bool is explicitly disallowed.
        with self.assertRaises(TypeError):
            patterns.broke_above(self.s, True)
        # And the series itself must be a Series.
        with self.assertRaises(TypeError):
            patterns.broke_above([1.0, 2.0], 1.5)

    def test_broke_below_rejects_bad_level(self) -> None:
        with self.assertRaises(TypeError):
            patterns.broke_below(self.s, None)

    def test_touched_above_rejects_bad_level(self) -> None:
        with self.assertRaises(TypeError):
            patterns.touched_above(self.s, {"x": 1})

    def test_touched_below_rejects_bad_level(self) -> None:
        with self.assertRaises(TypeError):
            patterns.touched_below(self.s, None)

    def test_bounced_from_rejects_bad_inputs(self) -> None:
        with self.assertRaises(TypeError):
            patterns.bounced_from([1.0], self.s, 100.0)
        with self.assertRaises(TypeError):
            patterns.bounced_from(self.s, [1.0], 100.0)
        with self.assertRaises(TypeError):
            patterns.bounced_from(self.s, self.s, None)
        with self.assertRaises(ValueError):
            patterns.bounced_from(self.s, self.s, 100.0, tol=-0.01)
        with self.assertRaises(TypeError):
            patterns.bounced_from(self.s, self.s, 100.0, tol="x")  # type: ignore[arg-type]

    def test_swing_high_rejects_non_series_and_negative_left(self) -> None:
        with self.assertRaises(TypeError):
            patterns.swing_high(self.s.tolist(), left=2, right=2)
        with self.assertRaises(ValueError):
            patterns.swing_high(self.s, left=-1, right=2)
        # left + right must be >= 1.
        with self.assertRaises(ValueError):
            patterns.swing_high(self.s, left=0, right=0)

    def test_swing_low_rejects_non_series_and_negative_right(self) -> None:
        with self.assertRaises(TypeError):
            patterns.swing_low(self.s.tolist(), left=2, right=2)
        with self.assertRaises(ValueError):
            patterns.swing_low(self.s, left=2, right=-1)

    def test_last_swing_high_level_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.last_swing_high_level(self.s.tolist(), left=2, right=2)
        with self.assertRaises(ValueError):
            patterns.last_swing_high_level(self.s, left=2, right=-1)

    def test_last_swing_low_level_rejects_non_series(self) -> None:
        with self.assertRaises(TypeError):
            patterns.last_swing_low_level(self.s.tolist(), left=2, right=2)

    def test_double_top_rejects_bad_tol(self) -> None:
        with self.assertRaises(TypeError):
            patterns.double_top(self.s.tolist(), tol=0.03)
        with self.assertRaises(ValueError):
            patterns.double_top(self.s, tol=-0.01)
        with self.assertRaises(TypeError):
            patterns.double_top(self.s, tol="x")  # type: ignore[arg-type]

    def test_double_bottom_rejects_bad_tol(self) -> None:
        with self.assertRaises(ValueError):
            patterns.double_bottom(self.s, tol=-0.01)
        with self.assertRaises(TypeError):
            patterns.double_bottom(self.s, tol=None)  # type: ignore[arg-type]

    def test_head_and_shoulders_rejects_bad_shoulder_tol(self) -> None:
        with self.assertRaises(ValueError):
            patterns.head_and_shoulders(self.s, shoulder_tol=-0.01)
        with self.assertRaises(TypeError):
            patterns.head_and_shoulders(self.s, shoulder_tol=None)  # type: ignore[arg-type]

    def test_triangle_rejects_non_series_and_bad_window(self) -> None:
        with self.assertRaises(TypeError):
            patterns.triangle(self.s.tolist(), self.s, window=20)
        with self.assertRaises(TypeError):
            patterns.triangle(self.s, self.s.tolist(), window=20)
        with self.assertRaises(ValueError):
            patterns.triangle(self.s, self.s, window=0)

    def test_broadening_rejects_non_series_and_bad_window(self) -> None:
        with self.assertRaises(TypeError):
            patterns.broadening(self.s.tolist(), self.s, window=20)
        with self.assertRaises(ValueError):
            patterns.broadening(self.s, self.s, window=-1)


# ---------------------------------------------------------------------------
# 5. End-to-end: a strategy source using patterns.* must compile + smoke.
# ---------------------------------------------------------------------------


_PATTERNS_DEMO_SOURCE = """
from doyoutrade.strategy_sdk import Strategy, Signal, patterns, indicators


class PatternsDemo(Strategy):
    timeframe = "1d"
    startup_history = 30

    def populate_indicators(self, df, ctx):
        df["hammer"] = patterns.is_hammer(df["open"], df["high"], df["low"], df["close"])
        df["bull_engulf"] = patterns.is_bullish_engulfing(
            df["open"], df["high"], df["low"], df["close"]
        )
        df["prior_hi"] = patterns.prior_high(df["high"], 20)
        df["broke"] = patterns.broke_above(df["close"], df["prior_hi"])
        return df

    def on_bar(self, df, ctx):
        if len(df) < self.startup_history:
            return Signal.hold()
        if bool(df["broke"].iloc[-1]) or bool(df["hammer"].iloc[-1]):
            return Signal.buy(tag="patterns_demo")
        return Signal.hold()
"""


class TestStrategySourceCompiles(unittest.TestCase):
    def test_patterns_demo_compiles_and_smokes(self) -> None:
        compiler = StrategyCompiler()
        result = compiler.validate_definition(_PATTERNS_DEMO_SOURCE, "PatternsDemo")
        self.assertTrue(result.success, msg=f"compile errors: {result.errors!r}")
        self.assertIsNotNone(result.artifact)
        smoke = compiler.smoke_test(result.artifact)
        self.assertTrue(smoke.success, msg=f"smoke errors: {smoke!r}")


if __name__ == "__main__":
    unittest.main()
