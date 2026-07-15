import unittest

from doyoutrade.backtest.engine import BarDrivenBacktestRunner


class BacktestEngineTests(unittest.TestCase):
    def test_runner_invokes_callback_for_each_bar(self):
        timeline = [
            "2026-01-01T09:31:00",
            "2026-01-01T09:32:00",
            "2026-01-01T09:33:00",
        ]
        seen = []

        def on_bar(as_of_time):
            seen.append(as_of_time)
            return {"submitted": 1, "vetoed": 0, "equity": 100_000.0 + len(seen) * 1000.0}

        runner = BarDrivenBacktestRunner(timeline=timeline, on_bar=on_bar)

        result = runner.run()

        self.assertEqual(seen, timeline)
        self.assertEqual(result.bars_processed, 3)
        self.assertEqual(result.submitted_count, 3)
        self.assertEqual(result.initial_equity, 101_000.0)
        self.assertEqual(result.final_equity, 103_000.0)
        self.assertAlmostEqual(result.total_return, (103_000.0 - 101_000.0) / 101_000.0)


if __name__ == "__main__":
    unittest.main()
