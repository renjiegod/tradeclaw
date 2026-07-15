"""Unit tests for :func:`doyoutrade.data.qmt_proxy.quote_snapshot_from_tick`.

Locks the 停牌 (suspended) handling: qmt returns ``last_price == 0`` for a
halted / no-trade-today symbol while ``last_close`` (昨收) stays positive.
The mapper must NOT derive a fake ``-100%`` from that sentinel — it must drop
the price, leave ``change`` / ``change_pct`` as ``None`` and flag the snapshot
``status="suspended"`` so the watchlist renders 停牌 instead of -100.00%.

Regression target: 中船特气 688146.SH showed -100.00% in the watchlist while
halted because ``last_price=0`` was treated as a real fill (see CLAUDE.md
§错误可见性 — schema sentinels must be made visible, never silently divided by).
"""

from __future__ import annotations

import unittest

from doyoutrade.data.qmt_proxy import quote_snapshot_from_tick


class QuoteSnapshotFromTickTests(unittest.TestCase):
    def test_normal_tick_derives_change_pct(self):
        snap = quote_snapshot_from_tick(
            "600519.SH",
            {"last_price": 21.0, "last_close": 20.0, "volume": 100.0},
        )
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.price, 21.0)
        self.assertEqual(snap.prev_close, 20.0)
        self.assertEqual(snap.change, 1.0)
        self.assertAlmostEqual(snap.change_pct, 5.0)

    def test_suspended_sentinel_last_price_zero(self):
        # qmt halt sentinel: last_price 0 alongside zero OHLCV, but a valid 昨收.
        snap = quote_snapshot_from_tick(
            "688146.SH",
            {
                "last_price": 0.0,
                "last_close": 19.16,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0.0,
                "amount": 0.0,
            },
        )
        self.assertEqual(snap.status, "suspended")
        # No fake -100%: price/change/change_pct are surfaced as unknown.
        self.assertIsNone(snap.price)
        self.assertIsNone(snap.change)
        self.assertIsNone(snap.change_pct)
        # prev_close + derived limit prices are still meaningful and kept.
        self.assertEqual(snap.prev_close, 19.16)
        self.assertIsNotNone(snap.limit_up_price)
        self.assertIsNotNone(snap.limit_down_price)

    def test_suspended_via_streamed_pre_close_key(self):
        # Streamed QuoteData shape carries ``pre_close`` instead of ``last_close``.
        snap = quote_snapshot_from_tick(
            "688146.SH", {"last_price": 0.0, "pre_close": 19.16}
        )
        self.assertEqual(snap.status, "suspended")
        self.assertIsNone(snap.change_pct)
        self.assertEqual(snap.prev_close, 19.16)

    def test_negative_last_price_is_also_treated_as_sentinel(self):
        # Defensive: a negative last price is never a real A-share fill either.
        snap = quote_snapshot_from_tick(
            "000001.SZ", {"last_price": -1.0, "last_close": 12.0}
        )
        self.assertEqual(snap.status, "suspended")
        self.assertIsNone(snap.price)
        self.assertIsNone(snap.change_pct)

    def test_missing_last_price_is_not_suspended(self):
        # A genuinely absent price (None) is not the halt sentinel — stays ok,
        # change_pct simply not derivable.
        snap = quote_snapshot_from_tick("600519.SH", {"last_close": 20.0})
        self.assertEqual(snap.status, "ok")
        self.assertIsNone(snap.price)
        self.assertIsNone(snap.change_pct)


if __name__ == "__main__":
    unittest.main()
