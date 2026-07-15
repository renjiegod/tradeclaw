from __future__ import annotations

import unittest

from doyoutrade.debug.overrides import PatchedDataProvider


class _FakeInner:
    def __init__(self) -> None:
        self.is_calls: list[str] = []
        self.dates_calls: list[tuple[str, str]] = []

    async def get_market_context(self):
        raise NotImplementedError

    async def get_bars(self, *args, **kwargs):
        raise NotImplementedError

    async def is_trading_day(self, date: str) -> bool:
        self.is_calls.append(date)
        return date == "2026-04-06"

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        self.dates_calls.append((start, end))
        return ["2026-04-06"]


class TestPatchedDataProvider(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_is_trading_day_and_get_trading_dates(self) -> None:
        inner = _FakeInner()
        patched = PatchedDataProvider(inner, {})
        self.assertTrue(await patched.is_trading_day("2026-04-06"))
        self.assertEqual(inner.is_calls, ["2026-04-06"])
        dates = await patched.get_trading_dates("2026-04-01", "2026-04-10")
        self.assertEqual(dates, ["2026-04-06"])
        self.assertEqual(inner.dates_calls, [("2026-04-01", "2026-04-10")])


if __name__ == "__main__":
    unittest.main()
