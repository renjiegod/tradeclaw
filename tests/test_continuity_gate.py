"""Integration tests for the write-time continuity gate in
``LocalHistoricalBarsDataProvider`` — the user invariant that a discontinuous
backfill must fail the whole write (no dirty/partial rows) while legitimate
suspensions/holidays are tolerated.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.core.models import Bar
from doyoutrade.data.cache_policy import DataCachePolicy
from doyoutrade.data.continuity import ContinuityError
from doyoutrade.data.local_market_bars import LocalHistoricalBarsDataProvider
from doyoutrade.data.protocols import (
    PROVIDER_NAME_BAOSTOCK,
    PROVIDER_NAME_QMT,
    ProviderCapabilities,
)

_CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def _bar(day: str, close: float = 10.0) -> Bar:
    return Bar(
        symbol="600000.SH",
        timestamp=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100.0,
        amount=1000.0,
        adjust_type="qfq",
    )


class FakeRepo:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.upsert_calls: list[dict[str, Any]] = []

    async def bars_in_range(self, **kw: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    async def upsert_bars(self, **kw: Any) -> int:
        self.upsert_calls.append(kw)
        return len(kw["bars"])


class FakeProvider:
    """Configurable upstream mimicking the data-stack contract.

    ``name`` + ``authoritative`` drive ``capabilities``; ``calendar`` is what
    ``get_trading_dates`` returns; ``suspended`` is forwarded as
    ``last_suspended_days`` (baostock-style per-date 停牌 signal).
    """

    def __init__(
        self,
        *,
        name: str,
        authoritative: bool,
        bar_days: list[str],
        calendar: list[str],
        suspended: set[str] | None = None,
    ) -> None:
        self.capabilities = ProviderCapabilities(
            name=name, authoritative_calendar=authoritative
        )
        self._bar_days = bar_days
        self._calendar = calendar
        self.last_used_provider = name
        self.last_suspended_days = set(suspended or set())
        self.get_bars_calls = 0

    async def get_bars(self, symbol, start, end, *, interval="1d", adjust="qfq"):
        self.get_bars_calls += 1
        return [_bar(d) for d in self._bar_days]

    async def get_trading_dates(self, start, end):
        return [d for d in self._calendar if start[:10] <= d <= end[:10]]


def _emit_patch():
    return patch(
        "doyoutrade.data.local_market_bars.emit_debug_event", new_callable=AsyncMock
    )


def _events(emit: AsyncMock, name: str) -> list[dict[str, Any]]:
    return [c.args[1] for c in emit.await_args_list if c.args[0] == name]


class ContinuityGateTests(unittest.IsolatedAsyncioTestCase):
    async def _get(self, provider: FakeProvider, repo: FakeRepo, policy=None):
        local = LocalHistoricalBarsDataProvider(
            repo, provider, provider="baostock", adjust="qfq", policy=policy
        )
        return await local.get_bars("600000.SH", "2026-01-05", "2026-01-09")

    async def test_authoritative_complete_persists(self) -> None:
        repo = FakeRepo()
        up = FakeProvider(
            name=PROVIDER_NAME_BAOSTOCK, authoritative=True, bar_days=_CAL, calendar=_CAL
        )
        with _emit_patch() as emit:
            bars = await self._get(up, repo)
        self.assertEqual(len(bars), 5)
        self.assertEqual(len(repo.upsert_calls), 1)
        self.assertTrue(_events(emit, "market_data.get_bars.continuity_passed"))

    async def test_confirmed_defect_rejects_and_does_not_write(self) -> None:
        repo = FakeRepo()
        # baostock authoritative, suspension source available, missing 01-07 NOT
        # a suspension → confirmed defect → reject whole write.
        up = FakeProvider(
            name=PROVIDER_NAME_BAOSTOCK,
            authoritative=True,
            bar_days=[d for d in _CAL if d != "2026-01-07"],
            calendar=_CAL,
            suspended=set(),
        )
        with _emit_patch() as emit:
            with self.assertRaises(ContinuityError):
                await self._get(up, repo)
        self.assertEqual(repo.upsert_calls, [])  # nothing persisted — no dirty data
        viol = _events(emit, "market_data.get_bars.continuity_violation")
        self.assertTrue(viol)
        self.assertEqual(viol[0]["continuity_classification"], "calendar_violation")
        self.assertIn("2026-01-07", viol[0]["missing_days_sample"])

    async def test_suspension_is_excluded_and_persists(self) -> None:
        repo = FakeRepo()
        up = FakeProvider(
            name=PROVIDER_NAME_BAOSTOCK,
            authoritative=True,
            bar_days=[d for d in _CAL if d != "2026-01-07"],
            calendar=_CAL,
            suspended={"2026-01-07"},  # the gap IS a known halt
        )
        with _emit_patch():
            bars = await self._get(up, repo)
        self.assertEqual(len(bars), 4)
        self.assertEqual(len(repo.upsert_calls), 1)  # suspension day excluded → ok

    async def test_unverifiable_gap_fails_by_default(self) -> None:
        repo = FakeRepo()
        # qmt: authoritative calendar but NO per-date suspension source → a gap
        # cannot be proven a halt → default on_unverifiable_gap=fail rejects.
        up = FakeProvider(
            name=PROVIDER_NAME_QMT,
            authoritative=True,
            bar_days=[d for d in _CAL if d != "2026-01-07"],
            calendar=_CAL,
        )
        with _emit_patch() as emit:
            with self.assertRaises(ContinuityError):
                await self._get(up, repo)
        self.assertEqual(repo.upsert_calls, [])
        viol = _events(emit, "market_data.get_bars.continuity_violation")
        self.assertEqual(viol[0]["rejected_by"], "on_unverifiable_gap=fail")

    async def test_unverifiable_gap_degrade_policy_persists(self) -> None:
        repo = FakeRepo()
        up = FakeProvider(
            name=PROVIDER_NAME_QMT,
            authoritative=True,
            bar_days=[d for d in _CAL if d != "2026-01-07"],
            calendar=_CAL,
        )
        policy = DataCachePolicy(on_unverifiable_gap="degrade")
        with _emit_patch() as emit:
            bars = await self._get(up, repo, policy=policy)
        self.assertEqual(len(bars), 4)
        self.assertEqual(len(repo.upsert_calls), 1)  # degrade → persisted
        deg = _events(emit, "market_data.get_bars.continuity_degraded")
        self.assertEqual(deg[0]["degraded_by"], "on_unverifiable_gap=degrade")

    async def test_non_authoritative_source_degrades(self) -> None:
        repo = FakeRepo()
        # akshare-like: not authoritative → calendar check skipped, internal-gap
        # only → small gap is accepted but flagged degraded.
        up = FakeProvider(
            name="akshare",
            authoritative=False,
            bar_days=[d for d in _CAL if d != "2026-01-07"],
            calendar=_CAL,
        )
        with _emit_patch() as emit:
            bars = await self._get(up, repo)
        self.assertEqual(len(repo.upsert_calls), 1)
        deg = _events(emit, "market_data.get_bars.continuity_degraded")
        self.assertTrue(deg)
        self.assertEqual(deg[0]["calendar_degraded_reason"], "non_authoritative_calendar")

    async def test_auto_backfill_disabled_skips_upstream(self) -> None:
        repo = FakeRepo()  # local miss
        up = FakeProvider(
            name=PROVIDER_NAME_BAOSTOCK, authoritative=True, bar_days=_CAL, calendar=_CAL
        )
        policy = DataCachePolicy(auto_backfill=False)
        with _emit_patch() as emit:
            bars = await self._get(up, repo, policy=policy)
        self.assertEqual(bars, [])
        self.assertEqual(up.get_bars_calls, 0)  # read-only: no upstream fetch
        self.assertEqual(repo.upsert_calls, [])
        miss = _events(emit, "market_data.get_bars.miss")
        self.assertEqual(miss[0]["reason"], "auto_backfill_disabled")


if __name__ == "__main__":
    unittest.main()
