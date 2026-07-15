"""E2E: ``stock_screen`` runs end-to-end against a real bootstrap.

Verifies the new operation is wired into ``build_cli_tool_registry`` against
the live runtime stack (so a missing import / registry typo would surface
here, not just in unit tests). Uses an injected in-memory data provider so
the test doesn't depend on QMT / akshare / network availability.

Per CLAUDE.md §测试要求 §E2E, new API endpoints must have an E2E run that
exercises the registry + operation through the same code path the HTTP
server uses. This file is intentionally small — exhaustive condition /
skip-path coverage lives in ``tests.test_stock_screen``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.api.cli_tools import build_cli_tool_registry
from doyoutrade.api.operations.stock_screen import StockScreenTool
from doyoutrade.core.models import Bar


def _run(coro):
    return asyncio.run(coro)


def _make_bars(
    symbol: str,
    *,
    count: int,
    closes: list[float],
    amounts: list[float] | None = None,
) -> list[Bar]:
    start = date(2025, 1, 1)
    return [
        Bar(
            symbol=symbol,
            timestamp=(start + timedelta(days=i)).isoformat(),
            open=closes[i] - 0.1,
            high=closes[i] + 0.2,
            low=closes[i] - 0.2,
            close=closes[i],
            volume=1000.0,
            amount=(amounts[i] if amounts is not None else None),
        )
        for i in range(count)
    ]


class _InMemoryProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = dict(bars_by_symbol)

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = "qfq",
    ) -> list[Bar]:
        return [
            b
            for b in self._bars.get(symbol, [])
            if start_time[:10] <= b.timestamp[:10] <= end_time[:10]
        ]

    async def aclose(self) -> None:
        pass


@unittest.skipUnless(e2e_enabled(), "DOYOUTRADE_E2E=1 not set; skipping e2e suite")
class StockScreenE2E(unittest.TestCase):
    def test_registry_contains_stock_screen_and_runs_through_bootstrap(self) -> None:
        async def _run_test() -> None:
            async with build_e2e_runtime(
                profile="isolated", model_mode=E2EModelMode.STUB
            ) as ctx:
                runtime = ctx.runtime
                # Build the same registry the HTTP server wires inside
                # build_app — this is the real check that bootstrap +
                # registry assembly stayed consistent after adding
                # stock_screen.
                registry = build_cli_tool_registry(
                    service=runtime["service"],
                    strategy_registry_service=runtime.get("strategy_registry_service"),
                    strategy_definition_repository=runtime.get("strategy_definition_repository"),
                    cron_manager=runtime.get("cron_manager"),
                    cron_run_repo=runtime.get("cron_run_repo"),
                    strategy_storage=runtime.get("strategy_storage"),
                    compiler=runtime.get("strategy_compiler"),
                )
                tool = registry.get("stock_screen")
                self.assertIsNotNone(tool, "stock_screen not registered in cli_tool_registry")
                assert tool is not None
                self.assertIsInstance(tool, StockScreenTool)

                # Inject the in-memory provider so the E2E doesn't depend
                # on QMT / akshare being configured. The factory shape
                # matches what _build_data_provider expects.
                bars = _make_bars(
                    "E2E.SH", count=400,
                    closes=[50.0 - i * 0.1 for i in range(400)],
                )
                provider = _InMemoryProvider({"E2E.SH": bars})
                tool._data_provider_factory = lambda _ds, _syms: provider  # type: ignore[attr-defined]

                with tempfile.TemporaryDirectory() as tmp:
                    output_path = os.path.join(tmp, "screen.csv")
                    result = await tool.execute(
                        universe=["E2E.SH", "MISSING.SH"],
                        asof="2026-01-20",
                        rsi_max=50.0,
                        output_path=output_path,
                    )

                    self.assertFalse(result.is_error, msg=result.text)
                    # Both the matched symbol and the skipped (no-bars) one
                    # are visible in the envelope — verifies the operation
                    # reaches its happy + skip paths through the real
                    # runtime.
                    self.assertIn("\"matched\": 1", result.text)
                    self.assertIn("\"skipped\": 1", result.text)
                    self.assertIn("\"symbol\": \"E2E.SH\"", result.text)
                    self.assertTrue(
                        Path(output_path).exists(),
                        msg=f"expected CSV at {output_path}",
                    )

                # Second pass: exercise the M1 atoms (ma_above_ma +
                # avg_amount) plus rank_by through the same real runtime so
                # a wiring/registry typo on the new flags surfaces here too.
                up_bars = _make_bars(
                    "BULL.SH", count=400,
                    closes=[10.0 + i * 0.3 for i in range(400)],
                    amounts=[2e9] * 400,
                )
                provider2 = _InMemoryProvider({"BULL.SH": up_bars})
                tool._data_provider_factory = lambda _ds, _syms: provider2  # type: ignore[attr-defined]
                with tempfile.TemporaryDirectory() as tmp:
                    output_path = os.path.join(tmp, "screen_rank.csv")
                    result = await tool.execute(
                        universe=["BULL.SH"],
                        asof="2026-01-20",
                        ma_above_ma="20,60",
                        ma_slope_min="20,5,0",
                        avg_amount_lookback=10,
                        avg_amount_min=1e9,
                        rank_by="rsi",
                        top_k=10,
                        output_path=output_path,
                    )
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"matched\": 1", result.text)
                    self.assertIn("\"symbol\": \"BULL.SH\"", result.text)
                    # New predicate + rank columns surface in the envelope.
                    self.assertIn("avg_amount", result.text)
                    self.assertIn("ma_slope20", result.text)

                # Third pass: --min-float-mv pulls the fundamentals axis. Inject
                # an in-memory provider so BIG passes (>=1e10) and SMALL fails.
                from doyoutrade.core.models import Fundamentals
                from doyoutrade.data.protocols import ProviderCapabilities

                class _Fund:
                    capabilities = ProviderCapabilities(name="akshare", supported_intervals=frozenset())

                    async def get_fundamentals_batch(self, symbols, *, asof=None):
                        caps = {"BIG.SH": 2e10, "SMALL.SH": 5e9}
                        return {
                            s: Fundamentals(code=s, float_mv=caps[s], provider="akshare")
                            for s in symbols if s in caps
                        }

                    async def get_fundamentals(self, symbol, *, asof=None):
                        return (await self.get_fundamentals_batch([symbol])).get(symbol)

                provider3 = _InMemoryProvider({
                    "BIG.SH": _make_bars("BIG.SH", count=400, closes=[10.0] * 400),
                    "SMALL.SH": _make_bars("SMALL.SH", count=400, closes=[10.0] * 400),
                })
                tool._data_provider_factory = lambda _ds, _syms: provider3  # type: ignore[attr-defined]
                tool._fundamentals_provider_factory = lambda _ds: _Fund()  # type: ignore[attr-defined]
                with tempfile.TemporaryDirectory() as tmp:
                    output_path = os.path.join(tmp, "screen_mv.csv")
                    result = await tool.execute(
                        universe=["BIG.SH", "SMALL.SH"],
                        asof="2026-01-20",
                        min_float_mv=1e10,
                        output_path=output_path,
                    )
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"matched\": 1", result.text)
                    self.assertIn("\"symbol\": \"BIG.SH\"", result.text)
                    self.assertIn("float_mv", result.text)

                # Fourth pass: --exclude-suspended pulls the event axis;
                # SUSP.SH is halted and dropped, OK.SH survives.
                from doyoutrade.core.models import EventItem

                class _Ev:
                    capabilities = ProviderCapabilities(name="akshare", supported_intervals=frozenset())

                    async def get_events_batch(self, symbols, *, asof=None):
                        return {
                            "SUSP.SH": [EventItem(code="SUSP.SH", event_type="suspension",
                                                  event_date=asof or "", detail="停牌", provider="akshare")]
                        } if "SUSP.SH" in symbols else {}

                    async def get_events(self, symbol, *, asof=None):
                        return (await self.get_events_batch([symbol], asof=asof)).get(symbol, [])

                provider4 = _InMemoryProvider({
                    "OK.SH": _make_bars("OK.SH", count=400, closes=[10.0] * 400),
                    "SUSP.SH": _make_bars("SUSP.SH", count=400, closes=[10.0] * 400),
                })
                tool._data_provider_factory = lambda _ds, _syms: provider4  # type: ignore[attr-defined]
                tool._event_provider_factory = lambda _ds: _Ev()  # type: ignore[attr-defined]
                with tempfile.TemporaryDirectory() as tmp:
                    output_path = os.path.join(tmp, "screen_susp.csv")
                    result = await tool.execute(
                        universe=["OK.SH", "SUSP.SH"],
                        asof="2026-01-20",
                        exclude_suspended=True,
                        output_path=output_path,
                    )
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"matched\": 1", result.text)
                    self.assertIn("\"symbol\": \"OK.SH\"", result.text)

                # Fifth pass: --scorer-file (code-screen) compiles a Strategy
                # SDK scorer and evaluates it over the universe through the
                # real StrategyCompiler + StrategyRunner (no run_cycle).
                scorer_src = (
                    "from __future__ import annotations\n"
                    "from doyoutrade.strategy_sdk import Strategy, Signal, indicators as ind\n\n\n"
                    "class Strategy(Strategy):\n"
                    "    name = 'e2e_above_ma'\n"
                    "    timeframe = '1d'\n"
                    "    startup_history = 20\n\n"
                    "    def on_bar(self, df, ctx) -> Signal:\n"
                    "        ma = ind.sma(df['close'], 20).iloc[-1]\n"
                    "        last = float(df['close'].iloc[-1])\n"
                    "        if last > float(ma):\n"
                    "            return Signal.buy(tag='above', diagnostics={'gap': last - float(ma)})\n"
                    "        return Signal.hold(tag='below')\n"
                )
                up = [10.0 + 0.5 * i for i in range(60)]
                down = [60.0 - 0.3 * i for i in range(60)]
                provider5 = _InMemoryProvider({
                    "UP.SH": _make_bars("UP.SH", count=60, closes=up),
                    "DN.SH": _make_bars("DN.SH", count=60, closes=down),
                })
                tool._data_provider_factory = lambda _ds, _syms: provider5  # type: ignore[attr-defined]
                with tempfile.TemporaryDirectory() as tmp:
                    scorer_path = os.path.join(tmp, "scorer.py")
                    Path(scorer_path).write_text(scorer_src, encoding="utf-8")
                    output_path = os.path.join(tmp, "screen_code.csv")
                    result = await tool.execute(
                        universe=["UP.SH", "DN.SH"],
                        asof="2025-03-01",
                        scorer_file=scorer_path,
                        rank_by_diagnostic="gap",
                        output_path=output_path,
                    )
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"mode\": \"code\"", result.text)
                    self.assertIn("\"matched\": 1", result.text)
                    self.assertIn("\"symbol\": \"UP.SH\"", result.text)

        _run(_run_test())


if __name__ == "__main__":
    unittest.main()
