import unittest

from tradeclaw.backtest.replay import ReplayClock
from tradeclaw.domain.models import OrderIntent
from tradeclaw.execution.adapters import SimulatedBrokerAdapter


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
            side="buy",
            quantity=100,
            amount=None,
            order_type="market",
            tif="day",
            strategy_tag="bt",
            price_reference=10.0,
            rationale="backtest",
        )

        import asyncio

        asyncio.run(broker.submit_intent(intent))

        self.assertEqual(len(broker.fills), 1)
        self.assertEqual(broker.fills[0].intent_id, "intent-3")


if __name__ == "__main__":
    unittest.main()
