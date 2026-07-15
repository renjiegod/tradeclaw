"""Tests for simulated-clock bar close overlay (MTM equity)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from doyoutrade.account.store_reader import StoreBackedAccountReader
from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.data.simulated_bar_marks import (
    backtest_mtm_seed_symbol_list,
    bar_close_for_cycle_time,
    bar_close_for_trading_day,
    merge_simulated_bar_marks_into_market,
    reset_mock_ledger_for_fresh_backtest,
    seed_mock_ledger_prices_for_cycle_time,
    seed_mock_ledger_prices_for_trading_day,
)
from doyoutrade.core.models import AccountSnapshot, Bar, MarketContext, PositionSnapshot
from doyoutrade.runtime.cycle_task import CycleTask, CycleTaskConfig


def _bar(sym: str, day: str, close: float) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=normalize_bar_timestamp(day),
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000_000.0,
    )


class SimulatedBarMarksTests(unittest.IsolatedAsyncioTestCase):
    async def test_bar_close_for_trading_day_finds_close(self) -> None:
        sym = "603778.SH"
        bars = [
            _bar(sym, "2026-01-05", 30.0),
            _bar(sym, "2026-01-06", 40.0),
            _bar(sym, "2026-01-07", 35.0),
        ]
        store = MockTradingDataProvider(bars_by_symbol={sym: bars})
        got = await bar_close_for_trading_day(
            store,
            symbol=sym,
            trading_day=datetime(2026, 1, 6, 12, 0, 0).date(),
        )
        self.assertEqual(got, 40.0)

    async def test_bar_close_for_cycle_time_5m_uses_exact_intraday_bar(self) -> None:
        sym = "603778.SH"
        bars = [
            _bar(sym, "2026-01-06T09:35:00", 30.0),
            _bar(sym, "2026-01-06T09:40:00", 40.0),
            _bar(sym, "2026-01-06T09:45:00", 35.0),
        ]
        store = MockTradingDataProvider(bars_by_symbol={sym: bars})
        got = await bar_close_for_cycle_time(
            store,
            symbol=sym,
            cycle_time=datetime(2026, 1, 6, 9, 40, 0),
            interval="5m",
        )
        self.assertEqual(got, 40.0)

    async def test_merge_updates_mock_store_and_market_context(self) -> None:
        sym = "603778.SH"
        day = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)
        bars = [_bar(sym, "2026-01-05", 10.0), _bar(sym, "2026-01-06", 20.0)]
        store = MockTradingDataProvider(
            cash=50_000.0,
            equity=50_000.0,
            positions=[PositionSnapshot(symbol=sym, quantity=100.0, cost_price=10.0)],
            bars_by_symbol={sym: bars},
        )
        reader = StoreBackedAccountReader(store)
        cfg = CycleTaskConfig(
            name="t",
            mode="backtest",
            universe=(sym,),
        )
        inst = CycleTask(config=cfg, worker=object(), task_id="i1")
        state = CycleRunState(
            run_id="r1",
            trace_id="t1",
            task_id="i1",
            agent_name="t",
            cycle_task=inst,
            clock_mode="simulated",
            cycle_time_utc=day.replace(tzinfo=None),
        )
        mc = MarketContext(symbol_to_price={sym: 99.0}, symbol_to_tick={})
        out = await merge_simulated_bar_marks_into_market(
            data_provider=store,
            account_reader=reader,
            cycle_state=state,
            market_context=mc,
            positions_preview=list(await reader.get_positions()),
        )
        self.assertEqual(out.symbol_to_price.get(sym), 20.0)
        snap = await store.get_account_snapshot()
        self.assertAlmostEqual(snap.equity, 50_000.0 + 100.0 * 20.0)

    async def test_get_account_snapshot_revalues_without_new_fills(self) -> None:
        sym = "603778.SH"
        store = MockTradingDataProvider(
            cash=10_000.0,
            equity=10_000.0,
            positions=[PositionSnapshot(symbol=sym, quantity=100.0, cost_price=10.0)],
            symbol_to_price={sym: 50.0},
        )
        before = await store.get_account_snapshot()
        self.assertAlmostEqual(before.equity, 10_000.0 + 100.0 * 50.0)
        store._symbol_to_price[sym] = 60.0
        after = await store.get_account_snapshot()
        self.assertAlmostEqual(after.equity, 10_000.0 + 100.0 * 60.0)

    async def test_reset_mock_ledger_clears_positions_but_keeps_bars(self) -> None:
        sym = "603778.SH"
        bars = [_bar(sym, "2026-01-05", 30.0), _bar(sym, "2026-01-06", 31.0)]
        store = MockTradingDataProvider(
            cash=50_000.0,
            equity=50_000.0,
            positions=[PositionSnapshot(symbol=sym, quantity=100.0, cost_price=12.0)],
            bars_by_symbol={sym: bars},
        )
        reader = StoreBackedAccountReader(store)
        self.assertTrue(reset_mock_ledger_for_fresh_backtest(reader))
        snap = await store.get_account_snapshot()
        self.assertAlmostEqual(snap.cash, 100_000.0)
        pos = await store.get_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].symbol, "600000.SH")
        self.assertEqual(pos[0].quantity, 0.0)
        got = await bar_close_for_trading_day(
            store,
            symbol=sym,
            trading_day=datetime(2026, 1, 6, 12, 0, 0).date(),
        )
        self.assertEqual(got, 31.0)

    async def test_seed_mock_ledger_prices_aligns_mtm_with_bar_close(self) -> None:
        sym = "603778.SH"
        bars = [_bar(sym, "2026-01-05", 30.0), _bar(sym, "2026-01-06", 40.0)]
        store = MockTradingDataProvider(
            cash=50_000.0,
            equity=50_000.0,
            positions=[PositionSnapshot(symbol=sym, quantity=100.0, cost_price=10.0)],
            bars_by_symbol={sym: bars},
        )
        reader = StoreBackedAccountReader(store)
        before = await reader.get_account_snapshot()
        self.assertAlmostEqual(before.equity, 50_000.0 + 100.0 * 10.0)
        await seed_mock_ledger_prices_for_trading_day(
            data_provider=store,
            account_reader=reader,
            trading_day=datetime(2026, 1, 6, 12, 0, 0).date(),
            symbols=[sym],
            bar_interval="1d",
        )
        after = await reader.get_account_snapshot()
        self.assertAlmostEqual(after.equity, 50_000.0 + 100.0 * 40.0)

    async def test_seed_mock_ledger_prices_for_cycle_time_uses_intraday_close(self) -> None:
        sym = "603778.SH"
        bars = [
            _bar(sym, "2026-01-06T09:35:00", 30.0),
            _bar(sym, "2026-01-06T09:40:00", 40.0),
        ]
        store = MockTradingDataProvider(
            cash=50_000.0,
            equity=50_000.0,
            positions=[PositionSnapshot(symbol=sym, quantity=100.0, cost_price=10.0)],
            bars_by_symbol={sym: bars},
        )
        reader = StoreBackedAccountReader(store)
        await seed_mock_ledger_prices_for_cycle_time(
            data_provider=store,
            account_reader=reader,
            cycle_time=datetime(2026, 1, 6, 9, 40, 0),
            symbols=[sym],
            bar_interval="5m",
        )
        after = await reader.get_account_snapshot()
        self.assertAlmostEqual(after.equity, 50_000.0 + 100.0 * 40.0)

    async def test_merge_overlays_market_context_even_without_mock_store(self) -> None:
        """Regression: when account_reader is real (e.g. QmtAccountReader during a
        backtest), the price overlay must still apply. Without it, wall-clock live
        quotes leak into the simulated cycle and corrupt order sizing / fill pricing.
        """
        sym = "603778.SH"
        day = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)
        bars = [_bar(sym, "2026-01-05", 10.0), _bar(sym, "2026-01-06", 20.0)]
        bars_provider = MockTradingDataProvider(bars_by_symbol={sym: bars})

        class _NonMockAccountReader:
            portfolio_source = "broker"

            async def get_account_snapshot(self):
                return AccountSnapshot(cash=Decimal("0"), equity=Decimal("0"))

            async def get_positions(self):
                return [PositionSnapshot(symbol=sym, quantity=100.0, cost_price=10.0)]

        cfg = CycleTaskConfig(
            name="t",
            mode="backtest",
            universe=(sym,),
        )
        inst = CycleTask(config=cfg, worker=object(), task_id="i1")
        state = CycleRunState(
            run_id="r1",
            trace_id="t1",
            task_id="i1",
            agent_name="t",
            cycle_task=inst,
            clock_mode="simulated",
            cycle_time_utc=day.replace(tzinfo=None),
        )
        reader = _NonMockAccountReader()
        positions = await reader.get_positions()
        mc = MarketContext(
            symbol_to_price={sym: 99.0},  # wall-clock live quote (wrong for cycle_time)
            symbol_to_tick={sym: {"close": 99.0}},
        )
        out = await merge_simulated_bar_marks_into_market(
            data_provider=bars_provider,
            account_reader=reader,
            cycle_state=state,
            market_context=mc,
            positions_preview=positions,
        )
        self.assertEqual(out.symbol_to_price.get(sym), 20.0)
        self.assertEqual((out.symbol_to_tick.get(sym) or {}).get("close"), 20.0)
        self.assertEqual((out.symbol_to_tick.get(sym) or {}).get("last"), 20.0)

    async def test_backtest_mtm_seed_symbol_list_universe_and_positions(self) -> None:
        sym = "603778.SH"
        store = MockTradingDataProvider(positions=[PositionSnapshot(symbol=sym, quantity=1.0, cost_price=1.0)])
        reader = StoreBackedAccountReader(store)
        cfg = CycleTaskConfig(
            name="t",
            mode="backtest",
            universe=(sym, "600000.SH"),
        )
        inst = CycleTask(config=cfg, worker=object(), task_id="i1")
        got = await backtest_mtm_seed_symbol_list(reader, inst)
        # Seed list is universe order plus held symbols (watch_symbols alone are not included).
        self.assertEqual(got, [sym, "600000.SH"])


if __name__ == "__main__":
    unittest.main()
