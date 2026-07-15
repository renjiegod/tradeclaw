import datetime
import unittest

from doyoutrade.core.models import AccountSnapshot, OrderIntent, PositionSnapshot
from doyoutrade.execution.risk import BasicRiskEngine, RiskConfig
from doyoutrade.execution.settlement import (
    aggregate_sellable_quantity,
    sell_intent_exceeds_sellable,
    settlement_mode,
    should_run_settlement_trigger_b,
    trading_day_from_cycle_time,
)


class SettlementModeTests(unittest.TestCase):
    def test_cn_a_share_ledger_is_t1(self) -> None:
        self.assertEqual(settlement_mode("cn_a_share", "ledger"), "t1")

    def test_broker_source_is_broker(self) -> None:
        self.assertEqual(settlement_mode("cn_a_share", "broker"), "broker")

    def test_other_profile_ledger_is_t0(self) -> None:
        self.assertEqual(settlement_mode("us_equity", "ledger"), "t0")


class SettlementTriggerBTests(unittest.TestCase):
    def test_first_day_records_marker_no_unlock(self) -> None:
        run, new_day = should_run_settlement_trigger_b(None, datetime.date(2024, 6, 3))
        self.assertFalse(run)
        self.assertEqual(new_day, "2024-06-03")

    def test_same_day_no_op(self) -> None:
        run, new_day = should_run_settlement_trigger_b("2024-06-03", datetime.date(2024, 6, 3))
        self.assertFalse(run)
        self.assertIsNone(new_day)

    def test_new_day_unlocks(self) -> None:
        run, new_day = should_run_settlement_trigger_b("2024-06-03", datetime.date(2024, 6, 4))
        self.assertTrue(run)
        self.assertEqual(new_day, "2024-06-04")


class AggregateSellableTests(unittest.TestCase):
    def test_t1_uses_available_only(self) -> None:
        positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=0,
            )
        ]
        sellable, legacy = aggregate_sellable_quantity(positions, "600000.SH", "t1")
        self.assertEqual(sellable, 0)
        self.assertFalse(legacy)

    def test_broker_min_of_qty_and_available(self) -> None:
        positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=40,
            )
        ]
        sellable, _ = aggregate_sellable_quantity(positions, "600000.SH", "broker")
        self.assertEqual(sellable, 40)

    def test_sell_intent_exceeds_sellable_t1(self) -> None:
        positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=0,
            )
        ]
        self.assertTrue(
            sell_intent_exceeds_sellable(100, positions, "600000.SH", "t1")
        )


class TradingDayTests(unittest.TestCase):
    def test_naive_cycle_time_uses_date(self) -> None:
        dt = datetime.datetime(2024, 6, 3, 7, 0, 0)
        self.assertEqual(trading_day_from_cycle_time(dt).isoformat(), "2024-06-03")


class BasicRiskSettlementTests(unittest.TestCase):
    def test_vetoes_sell_when_t1_available_zero(self) -> None:
        engine = BasicRiskEngine(
            RiskConfig(max_single_order_amount=None, max_position_ratio=1.0)
        )
        intent = OrderIntent(
            intent_id="oi-sell",
            symbol="600000.SH",
            action="sell",
            amount=100.0,
            order_type="market",
            tif="day",
            strategy_tag="t",
            price_reference=10.0,
            rationale="",
        )
        positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=0,
            )
        ]
        decisions = engine.evaluate(
            [intent],
            AccountSnapshot(cash=50_000.0, equity=100_000.0),
            positions,
            settlement_mode="t1",
        )
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "veto")
        self.assertEqual(decisions[0].reason, "settlement_sellable_exceeded")


if __name__ == "__main__":
    unittest.main()
