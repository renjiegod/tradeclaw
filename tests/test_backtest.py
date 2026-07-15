import asyncio
import unittest
from decimal import Decimal

from doyoutrade.backtest.replay import ReplayClock
from doyoutrade.account.store_reader import StoreBackedAccountReader
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.core.models import MarketContext, OrderIntent
from doyoutrade.execution.adapters import SimulatedBrokerAdapter


class BacktestTests(unittest.TestCase):
    def test_replay_clock_steps_in_order(self):
        clock = ReplayClock(["2026-01-01T09:31:00", "2026-01-01T09:32:00"])

        self.assertEqual(clock.current_time, "2026-01-01T09:31:00")
        self.assertTrue(clock.step())
        self.assertEqual(clock.current_time, "2026-01-01T09:32:00")
        self.assertFalse(clock.step())

    def test_simulated_broker_records_fill(self):
        broker = SimulatedBrokerAdapter()
        intent = OrderIntent(
            intent_id="intent-3",
            symbol="600000.SH",
            action="buy",
            amount=1000.0,
            order_type="market",
            tif="day",
            strategy_tag="bt",
            price_reference=10.0,
            rationale="backtest",
        )

        asyncio.run(broker.submit_intent(intent))

        self.assertEqual(len(broker.fills), 1)
        self.assertEqual(broker.fills[0].intent_id, "intent-3")

    def test_simulated_broker_uses_close_when_market_context_provided(self):
        broker = SimulatedBrokerAdapter()
        intent = OrderIntent(
            intent_id="intent-4",
            symbol="600000.SH",
            action="buy",
            amount=100.0,
            order_type="market",
            tif="day",
            strategy_tag="bt",
            price_reference=9.0,
            rationale="backtest",
        )
        mc = MarketContext(
            symbol_to_price={"600000.SH": 9.0},
            symbol_to_tick={"600000.SH": {"close": 10.25}},
        )

        async def go():
            return await broker.submit_intent(intent, market_context=mc)

        asyncio.run(go())
        self.assertEqual(broker.fills[0].price, 10.25)
        self.assertEqual(broker.fills[0].quantity, 9.0)

    def test_simulated_broker_updates_mock_store_when_ledger_wired(self):
        store = MockTradingDataProvider(
            symbol_to_price={"600000.SH": 10.0},
            cash=100_000.0,
            equity=100_000.0,
            positions=[],
        )
        broker = SimulatedBrokerAdapter(ledger=store)
        intent = OrderIntent(
            intent_id="intent-ledger",
            symbol="600000.SH",
            action="buy",
            amount=1000.0,
            order_type="market",
            tif="day",
            strategy_tag="bt",
            price_reference=10.0,
            rationale="ledger",
        )
        mc = MarketContext(symbol_to_price={"600000.SH": 10.0}, symbol_to_tick={})

        async def go():
            await broker.submit_intent(intent, market_context=mc)
            return await StoreBackedAccountReader(store).get_account_snapshot()

        snap = asyncio.run(go())
        self.assertEqual(snap.cash, Decimal("99000"))
        self.assertGreater(snap.equity, Decimal(0))


if __name__ == "__main__":
    unittest.main()
