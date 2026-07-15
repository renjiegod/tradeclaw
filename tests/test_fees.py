"""Tests for the A-share fee model + its ledger / FIFO integration.

Covers the fee-ON branches that the default-off golden tests
(test_backtest_summary*) deliberately can't reach: the fee math itself, the
config gate, the ledger cash deduction, and the full-口径 FIFO PnL net of
both legs. Default-off behaviour (no fee_model → numbers unchanged) is also
asserted here so the backward-compat guarantee is explicit.
"""

from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from doyoutrade.backtest.summary import FillRecord as SummaryFill, _fifo_match
from doyoutrade.core.models import FillRecord, OrderIntent
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.execution.fees import AShareFeeModel, fee_model_from_config


def _intent(action: str) -> OrderIntent:
    return OrderIntent(
        intent_id="oi-1",
        symbol="600000.SH",
        action=action,  # type: ignore[arg-type]
        amount=1000.0,
        order_type="market",
        tif="day",
        strategy_tag="t",
        price_reference=10.0,
        rationale="",
    )


def _sfill(side: str, qty: int, price: str, fee: str, *, day: int = 1) -> SummaryFill:
    return SummaryFill(
        symbol="600000.SH",
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        price=Decimal(price),
        timestamp=datetime.datetime(2024, 6, day),
        intent_id=None,
        cycle_run_id=f"cr-{day}",
        fee=Decimal(fee),
    )


class FeeModelMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = AShareFeeModel()  # defaults: 万2.5 + min5, stamp 0.05% sell, transfer 0.001%

    def test_buy_hits_min_commission(self) -> None:
        # notional 10000 → commission 2.5 floored to 5; transfer 0.1; no stamp
        self.assertEqual(self.m.compute_fee("buy", 1000, 10), Decimal("5.10"))

    def test_sell_adds_stamp_tax(self) -> None:
        # same notional + stamp 10000*0.0005=5.0 → 5 + 0.1 + 5.0
        self.assertEqual(self.m.compute_fee("sell", 1000, 10), Decimal("10.10"))

    def test_large_buy_uses_rate_commission(self) -> None:
        # notional 1_000_000 → commission 250, transfer 10, no stamp
        self.assertEqual(self.m.compute_fee("buy", 100000, 10), Decimal("260.00"))

    def test_large_sell_adds_stamp(self) -> None:
        # + stamp 1_000_000*0.0005 = 500 → 250 + 10 + 500
        self.assertEqual(self.m.compute_fee("sell", 100000, 10), Decimal("760.00"))

    def test_zero_notional_no_fee(self) -> None:
        self.assertEqual(self.m.compute_fee("buy", 0, 10), Decimal("0"))


class FeeConfigGateTests(unittest.TestCase):
    def test_none_and_empty_are_off(self) -> None:
        self.assertIsNone(fee_model_from_config(None))
        self.assertIsNone(fee_model_from_config({}))
        self.assertIsNone(fee_model_from_config("nope"))

    def test_enabled_false_is_off(self) -> None:
        self.assertIsNone(fee_model_from_config({"enabled": False, "commission_rate": 0.001}))

    def test_custom_rate_overrides_default(self) -> None:
        m = fee_model_from_config({"commission_rate": 0.0003, "min_commission": 0})
        assert m is not None
        self.assertEqual(m.commission_rate, Decimal("0.0003"))
        self.assertEqual(m.min_commission, Decimal("0"))
        # min_commission 0 → tiny order pays the rate, not a floor
        self.assertEqual(m.compute_fee("buy", 100, 10), Decimal("0.31"))  # 1000*0.0003=0.3 + transfer 0.01

    def test_negative_rate_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fee_model_from_config({"commission_rate": -0.001})


class LedgerFeeTests(unittest.TestCase):
    def _buy(self, store: MockTradingDataProvider) -> FillRecord:
        fill = FillRecord(intent_id="oi-1", symbol="600000.SH", side="buy", quantity=1000, price=10.0)
        store.apply_synthetic_fill(_intent("buy"), fill)
        return fill

    def test_default_off_cash_unchanged(self) -> None:
        store = MockTradingDataProvider(cash=100000.0, ledger_settlement_mode="t0")
        fill = self._buy(store)
        # no fee model → notional only, fill.fee stays 0.0
        self.assertEqual(store._cash, Decimal("90000"))
        self.assertEqual(fill.fee, 0.0)

    def test_fee_on_deducts_from_cash_and_marks_fill(self) -> None:
        store = MockTradingDataProvider(cash=100000.0, ledger_settlement_mode="t0")
        store.fee_model = AShareFeeModel()
        fill = self._buy(store)
        # 100000 - 10000 - 5.10
        self.assertEqual(store._cash, Decimal("89994.90"))
        self.assertEqual(fill.fee, 5.10)


class FeeConfigWiringTests(unittest.TestCase):
    """settings.fee_config → CycleTaskConfig.fee_config (the opt-in path)."""

    def _cfg(self, settings):
        from doyoutrade.runtime.cycle_task import cycle_task_config_from_params

        return cycle_task_config_from_params(
            name="t", mode="backtest", description="",
            data_provider="mock", universe=["600000.SH"],
            settings={"strategy": {"definition_id": "sd-x"}, **settings},
        )

    def test_absent_fee_config_is_none(self) -> None:
        self.assertIsNone(self._cfg({}).fee_config)

    def test_fee_config_parsed_from_settings(self) -> None:
        cfg = self._cfg({"fee_config": {"enabled": True, "commission_rate": 0.0003}})
        self.assertEqual(cfg.fee_config, {"enabled": True, "commission_rate": 0.0003})

    def test_non_dict_fee_config_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._cfg({"fee_config": "nope"})


class FifoFeeTests(unittest.TestCase):
    def test_closed_trade_pnl_net_of_both_legs(self) -> None:
        closed, _open = _fifo_match([
            _sfill("buy", 100, "10", "3", day=1),
            _sfill("sell", 100, "11", "4", day=2),
        ])
        self.assertEqual(len(closed), 1)
        # gross 100*(11-10)=100 ; fees 100*(3/100 + 4/100)=7 → 93
        self.assertEqual(closed[0].pnl, Decimal("93"))

    def test_partial_close_apportions_fees(self) -> None:
        closed, open_lots = _fifo_match([
            _sfill("buy", 100, "10", "3", day=1),
            _sfill("sell", 50, "11", "2", day=2),
        ])
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].qty, 50)
        # gross 50*1=50 ; fees 50*(3/100 + 2/50)=50*0.07=3.5 → 46.5
        self.assertEqual(closed[0].pnl, Decimal("46.5"))
        self.assertIn("600000.SH", open_lots)

    def test_fee_free_fills_unchanged(self) -> None:
        closed, _open = _fifo_match([
            _sfill("buy", 100, "10", "0", day=1),
            _sfill("sell", 100, "11", "0", day=2),
        ])
        self.assertEqual(closed[0].pnl, Decimal("100"))


if __name__ == "__main__":
    unittest.main()
