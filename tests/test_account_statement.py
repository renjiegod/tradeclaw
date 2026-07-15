"""``gather_account_statement`` — the live-account portion of the daily review.

Verifies decimal-string money, positions reuse of build_post_cycle_account,
graceful feature-detection of optional reader methods, and that a per-surface
fetch failure lands in ``errors`` (surfaced) without losing the other surfaces.
"""

import unittest
from datetime import date, datetime, timezone

from doyoutrade.account.statement import gather_account_statement
from doyoutrade.core.models import (
    AccountSnapshot,
    AssetSnapshot,
    PositionSnapshot,
    TradeSnapshot,
)

_ASOF = date(2026, 6, 17)
_CAP = datetime(2026, 6, 17, 7, 30, tzinfo=timezone.utc)


class _FullReader:
    portfolio_source = "broker"

    async def get_account_snapshot(self):
        return AccountSnapshot(cash=1000, equity=5000)

    async def get_positions(self):
        return [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                market_price=11,
                market_value=1100,
                available=100,
            )
        ]

    async def get_asset_snapshot(self):
        return AssetSnapshot(
            total_asset=5000,
            market_value=1100,
            cash=1000,
            frozen_cash=0,
            available_cash=1000,
            profit_loss=100,
            profit_loss_ratio=0.02,
        )

    async def get_trades(self, asof):
        return [
            TradeSnapshot(
                trade_id="t1",
                order_id="o1",
                symbol="600000.SH",
                side="BUY",
                quantity=100,
                price=10,
                amount=1000,
                trade_time="2026-06-17T10:00:00",
                commission=0.5,
            )
        ]


class _CoreOnlyReader:
    """A reader without the optional get_asset_snapshot / get_trades methods."""

    portfolio_source = "ledger"

    async def get_account_snapshot(self):
        return AccountSnapshot(cash=0, equity=0)

    async def get_positions(self):
        return []


class _TradesFailReader(_FullReader):
    async def get_trades(self, asof):
        raise RuntimeError("qmt trades timeout")


class AccountStatementTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_statement_decimal_strings(self):
        st = await gather_account_statement(_FullReader(), asof=_ASOF, captured_at=_CAP)
        self.assertEqual(st["asof"], "2026-06-17")
        self.assertEqual(st["account"]["account"]["cash"], "1000")
        self.assertEqual(st["account"]["account"]["equity"], "5000")
        self.assertEqual(len(st["account"]["positions"]), 1)
        # money are strings, not floats
        self.assertIsInstance(st["asset"]["total_asset"], str)
        self.assertEqual(st["asset"]["frozen_cash"], "0")
        self.assertEqual(st["trade_count"], 1)
        self.assertEqual(st["trades"][0]["price"], "10")
        self.assertEqual(st["errors"], [])

    async def test_optional_methods_absent_degrades_gracefully(self):
        st = await gather_account_statement(_CoreOnlyReader(), asof=_ASOF, captured_at=_CAP)
        self.assertIsNotNone(st["account"])
        self.assertIsNone(st["asset"])
        self.assertEqual(st["trades"], [])
        self.assertEqual(st["trade_count"], 0)
        self.assertEqual(st["errors"], [])  # absence is not an error

    async def test_per_surface_failure_is_surfaced_not_swallowed(self):
        st = await gather_account_statement(_TradesFailReader(), asof=_ASOF, captured_at=_CAP)
        # account + asset still present
        self.assertIsNotNone(st["account"])
        self.assertIsNotNone(st["asset"])
        # the trades failure is surfaced structurally
        self.assertEqual(len(st["errors"]), 1)
        self.assertEqual(st["errors"][0]["stage"], "trades")
        self.assertEqual(st["errors"][0]["error_type"], "RuntimeError")
        self.assertIn("hint", st["errors"][0])


if __name__ == "__main__":
    unittest.main()
