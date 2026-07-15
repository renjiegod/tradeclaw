"""Tests for the suspension-vs-gap distinction in the backtest mark overlay.

A baostock-sourced universe candidate can be missing its cycle-day bar for two
very different reasons: a genuine trading halt (``tradestatus==0``, no tradeable
bar) or a plain upstream data gap (the symbol traded but the row is absent).
The fix:

* persists the halt signal next to the cached bars
  (:class:`CachedBarSuspensionRecord` / store ``suspended_days``),
* carries the last close forward for a *data gap* so the buy can price
  (``carry_forward``, ``tradeable=True``),
* still marks a *halt* for MTM but refuses to fill a buy
  (``suspended_carry_forward``, ``tradeable=False`` → PositionManager
  ``symbol_suspended``),

instead of the legacy silent ``continue`` that produced ``no_reference_price``
zero-fills for both cases.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doyoutrade.account.store_reader import StoreBackedAccountReader
from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.models import Bar, MarketContext, PositionSnapshot
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.bars_cache_store import InMemoryBarsCacheStore
from doyoutrade.data.cached_bars import CachedBarsDataProvider
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.data.simulated_bar_marks import (
    MARK_CARRY_FORWARD,
    MARK_EXACT,
    MARK_NO_DATA,
    MARK_SUSPENDED_CARRY_FORWARD,
    MARK_SUSPENDED_NO_PRIOR,
    merge_simulated_bar_marks_into_market,
    resolve_mark_for_cycle_time,
)
from doyoutrade.execution.position_manager import PositionConstraints, PositionManager
from doyoutrade.execution.position_manager import PositionSignal as Signal
from doyoutrade.persistence.db import (
    Base,
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.repositories import SqlAlchemyCachedBarsRepository
from doyoutrade.runtime.cycle_task import CycleTask, CycleTaskConfig
from decimal import Decimal


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


class _FakeBarsProvider:
    """Minimal data provider exposing get_bars + suspended_days_in_range."""

    def __init__(self, bars: list[Bar], suspended: set[str] | None = None) -> None:
        self._bars = bars
        self._suspended = {d[:10] for d in (suspended or set())}

    async def get_bars(
        self, symbol: str, start: str, end: str, *, interval: str = "1d", adjust: str = "qfq"
    ) -> list[Bar]:
        lo, hi = start[:10], end[:10]
        return [b for b in self._bars if lo <= normalize_bar_timestamp(b.timestamp)[:10] <= hi]

    async def suspended_days_in_range(
        self, symbol: str, start: str, end: str, *, interval: str = "1d", adjust: str = "qfq"
    ) -> set[str]:
        return {d for d in self._suspended if start[:10] <= d <= end[:10]}


SYM = "603778.SH"
CYCLE = datetime(2026, 1, 6, 12, 0, 0)


class ResolveMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_day_bar(self) -> None:
        prov = _FakeBarsProvider([_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-06", 40.0)])
        m = await resolve_mark_for_cycle_time(prov, symbol=SYM, cycle_time=CYCLE)
        self.assertEqual(m.source, MARK_EXACT)
        self.assertEqual(m.close, 40.0)
        self.assertTrue(m.tradeable)
        self.assertEqual(m.staleness_days, 0)

    async def test_data_gap_carries_forward_and_is_tradeable(self) -> None:
        # No bar on 2026-01-06, not flagged as a halt → gap → carry-forward 30.0.
        prov = _FakeBarsProvider([_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-07", 35.0)])
        m = await resolve_mark_for_cycle_time(prov, symbol=SYM, cycle_time=CYCLE)
        self.assertEqual(m.source, MARK_CARRY_FORWARD)
        self.assertEqual(m.close, 30.0)
        self.assertTrue(m.tradeable)
        self.assertEqual(m.as_of_day, "2026-01-05")
        self.assertEqual(m.staleness_days, 1)

    async def test_halt_carries_forward_but_not_tradeable(self) -> None:
        prov = _FakeBarsProvider(
            [_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-07", 35.0)],
            suspended={"2026-01-06"},
        )
        m = await resolve_mark_for_cycle_time(prov, symbol=SYM, cycle_time=CYCLE)
        self.assertEqual(m.source, MARK_SUSPENDED_CARRY_FORWARD)
        self.assertEqual(m.close, 30.0)  # marked for MTM
        self.assertFalse(m.tradeable)  # but buy must not fill

    async def test_halt_with_no_prior_close(self) -> None:
        prov = _FakeBarsProvider([_bar(SYM, "2026-01-07", 35.0)], suspended={"2026-01-06"})
        m = await resolve_mark_for_cycle_time(prov, symbol=SYM, cycle_time=CYCLE)
        self.assertEqual(m.source, MARK_SUSPENDED_NO_PRIOR)
        self.assertIsNone(m.close)
        self.assertFalse(m.tradeable)

    async def test_no_data_at_or_before_cycle_day(self) -> None:
        prov = _FakeBarsProvider([_bar(SYM, "2026-01-07", 35.0)])
        m = await resolve_mark_for_cycle_time(prov, symbol=SYM, cycle_time=CYCLE)
        self.assertEqual(m.source, MARK_NO_DATA)
        self.assertIsNone(m.close)


class MergeOverlayTradeableFlagTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, universe: tuple[str, ...]) -> CycleRunState:
        cfg = CycleTaskConfig(name="t", mode="backtest", universe=universe)
        inst = CycleTask(config=cfg, worker=object(), task_id="i1")
        return CycleRunState(
            run_id="r1",
            trace_id="t1",
            task_id="i1",
            agent_name="t",
            cycle_task=inst,
            clock_mode="simulated",
            cycle_time_utc=CYCLE,
        )

    async def test_gap_symbol_priced_and_tradeable(self) -> None:
        prov = _FakeBarsProvider([_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-07", 35.0)])
        reader = StoreBackedAccountReader(MockTradingDataProvider())
        out = await merge_simulated_bar_marks_into_market(
            data_provider=prov,
            account_reader=reader,
            cycle_state=self._state((SYM,)),
            market_context=MarketContext(),
            positions_preview=[],
        )
        self.assertEqual(out.symbol_to_price.get(SYM), 30.0)
        tick = out.symbol_to_tick.get(SYM) or {}
        self.assertTrue(tick.get("tradeable"))
        self.assertEqual(tick.get("mark_source"), MARK_CARRY_FORWARD)

    async def test_halt_symbol_marked_but_not_tradeable(self) -> None:
        prov = _FakeBarsProvider(
            [_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-07", 35.0)],
            suspended={"2026-01-06"},
        )
        reader = StoreBackedAccountReader(MockTradingDataProvider())
        out = await merge_simulated_bar_marks_into_market(
            data_provider=prov,
            account_reader=reader,
            cycle_state=self._state((SYM,)),
            market_context=MarketContext(),
            positions_preview=[],
        )
        self.assertEqual(out.symbol_to_price.get(SYM), 30.0)  # MTM mark present
        tick = out.symbol_to_tick.get(SYM) or {}
        self.assertIs(tick.get("tradeable"), False)
        self.assertEqual(tick.get("mark_source"), MARK_SUSPENDED_CARRY_FORWARD)


class PositionManagerSuspensionGateTests(unittest.TestCase):
    def _account(self, cash: float, equity: float):
        from doyoutrade.core.models import AccountSnapshot

        return AccountSnapshot(cash=Decimal(str(cash)), equity=Decimal(str(equity)))

    def test_gap_symbol_buys(self) -> None:
        pm = PositionManager(PositionConstraints())
        mc = MarketContext(symbol_to_tick={"600000": {"close": 10.0, "tradeable": True}})
        out = pm.compute_intents([Signal(symbol="600000", value=1)], self._account(10000, 10000), [], mc)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")

    def test_halt_symbol_skipped_with_symbol_suspended(self) -> None:
        pm = PositionManager(PositionConstraints())
        # Price present (carried forward) but flagged not tradeable → no fill.
        mc = MarketContext(symbol_to_tick={"600000": {"close": 10.0, "tradeable": False}})
        out = pm.compute_intents([Signal(symbol="600000", value=1)], self._account(10000, 10000), [], mc)
        self.assertEqual(out, [])


class SuspensionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyCachedBarsRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_record_and_query_suspended_days(self) -> None:
        await self.repo.record_fetch(
            provider="baostock",
            symbol=SYM,
            interval="1d",
            start="2026-01-01",
            end="2026-01-10",
            bars=[{"symbol": SYM, "timestamp": "2026-01-05", "open": 1, "high": 1, "low": 1, "close": 30.0, "volume": 1}],
            suspended_days={"2026-01-06", "2026-01-07"},
        )
        got = await self.repo.suspended_days_in_range(
            provider="baostock", symbol=SYM, interval="1d", start="2026-01-01", end="2026-01-06"
        )
        self.assertEqual(got, {"2026-01-06"})  # 01-07 is outside the queried range
        got_all = await self.repo.suspended_days_in_range(
            provider="baostock", symbol=SYM, interval="1d", start="2026-01-01", end="2026-01-10"
        )
        self.assertEqual(got_all, {"2026-01-06", "2026-01-07"})

    async def test_invalidate_clears_suspensions(self) -> None:
        await self.repo.record_fetch(
            provider="baostock",
            symbol=SYM,
            interval="1d",
            start="2026-01-01",
            end="2026-01-10",
            bars=[],
            suspended_days={"2026-01-06"},
        )
        await self.repo.invalidate_symbol_cache(provider="baostock", symbol=SYM, interval="1d")
        got = await self.repo.suspended_days_in_range(
            provider="baostock", symbol=SYM, interval="1d", start="2026-01-01", end="2026-01-10"
        )
        self.assertEqual(got, set())


class _SuspendingInner:
    """Fake inner provider that reports a halt via ``last_suspended_days``."""

    capabilities = None

    def __init__(self) -> None:
        self.last_suspended_days: set[str] = set()
        self._bars = [_bar(SYM, "2026-01-05", 30.0), _bar(SYM, "2026-01-07", 35.0)]

    async def get_bars(
        self, symbol: str, start: str, end: str, *, interval: str = "1d", adjust: str = "qfq"
    ) -> list[Bar]:
        # baostock drops the halted 2026-01-06 row but records it as suspended.
        self.last_suspended_days = {"2026-01-06"}
        lo, hi = start[:10], end[:10]
        return [b for b in self._bars if lo <= normalize_bar_timestamp(b.timestamp)[:10] <= hi]


class CachedProviderSuspensionCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_persists_and_survives_cache_hit(self) -> None:
        inner = _SuspendingInner()
        store = InMemoryBarsCacheStore()
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="r1", store=store)
        # First fetch (miss) captures the halt.
        await provider.get_bars(SYM, "2026-01-01", "2026-01-10", interval="1d")
        susp = await provider.suspended_days_in_range(SYM, "2026-01-01", "2026-01-10", interval="1d")
        self.assertEqual(susp, {"2026-01-06"})
        # A pure cache hit (no inner re-fetch) still resolves the halt.
        susp_hit = await provider.suspended_days_in_range(SYM, "2026-01-04", "2026-01-08", interval="1d")
        self.assertEqual(susp_hit, {"2026-01-06"})


if __name__ == "__main__":
    unittest.main()
