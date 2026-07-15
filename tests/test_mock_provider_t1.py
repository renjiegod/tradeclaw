import datetime
import unittest

from doyoutrade.core.models import FillRecord, OrderIntent, PositionSnapshot
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.execution.position_manager import PositionManager, PositionSignal
from doyoutrade.core.models import AccountSnapshot, MarketContext
from doyoutrade.money.decimal_helpers import decimal_from_number


class MockProviderT1Tests(unittest.TestCase):
    def test_buy_same_day_does_not_increase_available(self) -> None:
        store = MockTradingDataProvider(ledger_settlement_mode="t1")
        store.apply_settlement_trigger_b(datetime.date(2024, 6, 3))
        intent = OrderIntent(
            intent_id="oi-1",
            symbol="600000.SH",
            action="buy",
            amount=1000.0,
            order_type="market",
            tif="day",
            strategy_tag="t",
            price_reference=10.0,
            rationale="",
        )
        fill = FillRecord(
            intent_id="oi-1",
            symbol="600000.SH",
            side="buy",
            quantity=100,
            price=10.0,
        )
        store.apply_synthetic_fill(intent, fill)
        pos = store._positions[0]
        self.assertEqual(int(pos.quantity), 100)
        self.assertEqual(int(pos.available or 0), 0)

    def test_sell_rejected_when_no_available(self) -> None:
        store = MockTradingDataProvider(ledger_settlement_mode="t1")
        store._positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=0.0,
            )
        ]
        intent = OrderIntent(
            intent_id="oi-2",
            symbol="600000.SH",
            action="sell",
            amount=100.0,
            order_type="market",
            tif="day",
            strategy_tag="t",
            price_reference=10.0,
            rationale="",
        )
        fill = FillRecord(
            intent_id="oi-2",
            symbol="600000.SH",
            side="sell",
            quantity=100,
            price=10.0,
        )
        store.apply_synthetic_fill(intent, fill)
        self.assertEqual(int(store._positions[0].quantity), 100)

    def test_trigger_b_unlocks_next_day(self) -> None:
        store = MockTradingDataProvider(ledger_settlement_mode="t1")
        store.apply_settlement_trigger_b(datetime.date(2024, 6, 3))
        buy = OrderIntent(
            intent_id="oi-1",
            symbol="600000.SH",
            action="buy",
            amount=1000.0,
            order_type="market",
            tif="day",
            strategy_tag="t",
            price_reference=10.0,
            rationale="",
        )
        store.apply_synthetic_fill(
            buy,
            FillRecord(
                intent_id="oi-1",
                symbol="600000.SH",
                side="buy",
                quantity=100,
                price=10.0,
            ),
        )
        store.apply_settlement_trigger_b(datetime.date(2024, 6, 4))
        pos = next(p for p in store._positions if p.symbol == "600000.SH")
        self.assertEqual(int(pos.available or 0), 100)

    def test_position_manager_skips_same_day_sell(self) -> None:
        pm = PositionManager(settlement_mode="t1")
        positions = [
            PositionSnapshot(
                symbol="600000.SH",
                quantity=100,
                cost_price=10,
                available=0.0,
            )
        ]
        intents = pm.compute_intents(
            [PositionSignal(symbol="600000.SH", value=0)],
            AccountSnapshot(cash=decimal_from_number(0), equity=decimal_from_number(100_000)),
            positions,
            MarketContext(symbol_to_price={"600000.SH": 10.0}),
            settlement_mode="t1",
        )
        self.assertEqual(intents, [])


if __name__ == "__main__":
    unittest.main()
