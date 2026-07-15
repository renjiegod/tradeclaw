"""Unit tests for ``doyoutrade.api.operations.stock_screen``.

Coverage:

* Each condition family has at least one positive + negative path.
* Per-symbol skip path emits the ``screener_symbol_skipped`` debug event
  with the expected ``reason`` (insufficient history, no bars, fetch error,
  evaluation raised).
* Top-k + sort + envelope shape match the contract that the CLI / agent
  consumers parse.
* ``execute(**kwargs)`` rejects unknown top-level keys (kwargs contract)
  and structurally-invalid values with stable error codes.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.api.operations.stock_screen import (
    StockScreenTool,
    _LocalFirstScreenProvider,
)
from doyoutrade.core.models import Bar


# ---------------------------------------------------------------------------
# Fake data provider — produces deterministic bars per symbol.
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]):
        self._bars_by_symbol = dict(bars_by_symbol)
        self.calls: list[tuple[str, str, str, str]] = []
        self.closed = False
        self.fail_symbols: set[str] = set()

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = "qfq",
    ) -> list[Bar]:
        self.calls.append((symbol, start_time, end_time, interval))
        if symbol in self.fail_symbols:
            raise RuntimeError(f"simulated provider failure for {symbol}")
        bars = list(self._bars_by_symbol.get(symbol, []))
        # Filter by [start, end] so the operation always sees a realistic
        # window slice (the real provider does the same).
        return [b for b in bars if start_time[:10] <= b.timestamp[:10] <= end_time[:10]]

    async def aclose(self) -> None:
        self.closed = True


def _make_bars(
    symbol: str,
    *,
    start: date,
    count: int,
    base_price: float = 10.0,
    trend: float = 0.0,
    volumes: list[float] | None = None,
    closes: list[float] | None = None,
    amounts: list[float] | None = None,
) -> list[Bar]:
    """Generate ``count`` daily bars starting at ``start``.

    Each bar's close = base_price + i * trend (or ``closes[i]`` if given);
    open/high/low are derived from close so OHLC stays internally consistent.
    ``amounts[i]`` sets the per-bar turnover (成交额); left ``None`` so existing
    callers keep the model default (``amount=None``).
    """

    bars: list[Bar] = []
    for i in range(count):
        if closes is not None:
            close = closes[i]
        else:
            close = base_price + i * trend
        # Skip weekends so the generated calendar looks like a trading session.
        bar_date = start + timedelta(days=i)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=bar_date.isoformat(),
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=float(close),
                volume=float(volumes[i] if volumes is not None else 1000.0),
                amount=(float(amounts[i]) if amounts is not None else None),
            )
        )
    return bars


def _factory_from(provider: _FakeProvider):
    def _build(_data_source: str, _symbols: list[str]):
        return provider

    return _build


def _extract_payload(tool_result) -> dict[str, Any]:
    """Pull the fenced-JSON payload back out of a ToolResult.text."""

    match = re.search(r"```json\n(.*)\n```", tool_result.text, re.DOTALL)
    assert match is not None, f"no fenced JSON in text: {tool_result.text!r}"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ReadUniverseFileTests(unittest.TestCase):
    """The CLI-side ``--universe-file`` reader for ``stock screen``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, body: str) -> str:
        path = Path(self._tmp.name) / "u.csv"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_plain_one_symbol_per_line(self) -> None:
        from doyoutrade.cli.commands.stock import _read_universe_file

        path = self._write("# comment\n600519.SH\n\n000001.SZ\n")
        self.assertEqual(_read_universe_file(path), ["600519.SH", "000001.SZ"])

    def test_skips_csv_header_and_takes_first_column(self) -> None:
        # A pandas ``to_csv`` dump (``symbol,name`` header + ``CODE,中文名``
        # rows) must not surface the header literal as a symbol
        # (tmp/messages.json turn 10).
        from doyoutrade.cli.commands.stock import _read_universe_file

        path = self._write("symbol,name\n600519.SH,贵州茅台\n000001.SZ,平安银行\n")
        self.assertEqual(_read_universe_file(path), ["600519.SH", "000001.SZ"])

    def test_non_header_first_row_is_kept(self) -> None:
        # Only a recognised header token is skipped; a real symbol on line 1
        # must be preserved.
        from doyoutrade.cli.commands.stock import _read_universe_file

        path = self._write("600519.SH,贵州茅台\n000001.SZ\n")
        self.assertEqual(_read_universe_file(path), ["600519.SH", "000001.SZ"])


class StockScreenContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_top_level_kwarg(self) -> None:
        tool = StockScreenTool()
        result = await tool.execute(universe=["A"], totally_made_up=1)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_rejects_empty_universe(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(universe=[], rsi_max=50.0)
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_universe]", result.text)

    async def test_rejects_non_string_universe_entry(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(universe=["600000.SH", 12345], rsi_max=50.0)
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_universe]", result.text)

    async def test_rejects_no_conditions(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(universe=["600000.SH"])
        self.assertTrue(result.is_error)
        self.assertIn("[error:no_conditions_specified]", result.text)

    async def test_rejects_invalid_asof(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(universe=["A"], asof="not-a-date", rsi_max=50.0)
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_date]", result.text)

    async def test_rejects_conflicting_rsi_bounds(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(
            universe=["A"], asof="2026-05-26", rsi_min=80.0, rsi_max=20.0
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_conditions]", result.text)

    async def test_rejects_unknown_pattern_name(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(
            universe=["A"], asof="2026-05-26", patterns="hammer,not_a_pattern"
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_pattern_name]", result.text)

    async def test_rejects_malformed_ma_cross(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(
            universe=["A"], asof="2026-05-26", ma_cross="golden:60,20"
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_condition_value]", result.text)

    async def test_rejects_volume_ratio_lookback_without_threshold(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(_FakeProvider({})))
        result = await tool.execute(
            universe=["A"], asof="2026-05-26", volume_ratio_lookback=5
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_condition_value]", result.text)


class StockScreenConditionTests(unittest.IsolatedAsyncioTestCase):
    """One positive + one negative case per condition family.

    All scenarios use a controlled universe so the math is verifiable by hand
    rather than relying on a real data provider.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        # asof picks the right edge of the generated window so each symbol's
        # last bar is the decision bar.
        self.start = date(2025, 1, 1)
        self.asof = self.start + timedelta(days=399)

    async def _run(
        self,
        provider: _FakeProvider,
        **kwargs: Any,
    ):
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        result = await tool.execute(
            universe=["MATCH.SH", "MISS.SH"],
            asof=self.asof.isoformat(),
            output_path=str(self.output_path),
            **kwargs,
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        return result, payload

    async def test_rsi_max_matches_oversold_only(self) -> None:
        # MATCH closes drift down (oversold), MISS stays flat / drifts up.
        match_closes = [50.0 - i * 0.1 for i in range(400)]
        miss_closes = [50.0 + i * 0.1 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        result, payload = await self._run(provider, rsi_max=30.0, rsi_period=14)

        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")
        self.assertIn("rsi", payload["preview"][0])

    async def test_rsi_min_matches_overbought_only(self) -> None:
        match_closes = [50.0 + i * 0.5 for i in range(400)]
        miss_closes = [50.0 - i * 0.1 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, rsi_min=70.0, rsi_period=14)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")

    async def test_pct_change_min_matches_only_strong_runner(self) -> None:
        # MATCH = +10% over last 5 bars; MISS = flat.
        match_closes = [10.0] * 395 + [10.0, 10.5, 11.0, 11.3, 11.5]
        miss_closes = [10.0] * 400
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, pct_change_lookback=5, pct_change_min=0.05)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertGreaterEqual(row["pct_change"], 0.05)

    async def test_volume_ratio_min_matches_volume_spike(self) -> None:
        # MATCH has a 5x volume spike on the last bar.
        normal_vols = [1000.0] * 399 + [5000.0]
        flat_vols = [1000.0] * 400
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, volumes=normal_vols),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, volumes=flat_vols),
            }
        )
        _, payload = await self._run(
            provider,
            volume_ratio_lookback=5,
            volume_ratio_min=3.0,
        )
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")

    async def test_close_at_high_window_detects_recent_high(self) -> None:
        # MATCH: monotonically increasing → last bar IS the 20-bar max.
        match_closes = [10.0 + i * 0.05 for i in range(400)]
        # MISS: flat then dip on last bar so close != 20-bar max.
        miss_closes = [10.0] * 399 + [9.5]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, close_at_high_window=20)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")

    async def test_price_above_ma_filters_correctly(self) -> None:
        # MATCH trending up so close > MA(20); MISS trending down so close < MA(20).
        match_closes = [10.0 + i * 0.5 for i in range(400)]
        miss_closes = [50.0 - i * 0.1 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, price_above_ma=20)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")

    async def test_kdj_golden_cross_matches_recent_upturn(self) -> None:
        # MATCH: a long decline that bottoms and snaps up in the last few
        # bars so K crosses above D inside --cross-window. MISS: a steady
        # steep decline all the way to asof so K stays pinned below D.
        match_closes = [200.0 - i * 0.5 for i in range(397)] + [4.0, 9.0, 16.0]
        miss_closes = [200.0 - i * 0.5 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, kdj="golden_cross", kdj_n=9, cross_window=5)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertIn("kdj_k", row)
        self.assertIn("kdj_d", row)
        self.assertIn("kdj_j", row)
        self.assertIn("kdj:golden_cross:9", row["matched_conditions"])

    async def test_cci_max_filters_oversold_only(self) -> None:
        # MATCH drifts down → CCI deeply negative; MISS drifts up → CCI positive.
        match_closes = [50.0 - i * 0.1 for i in range(400)]
        miss_closes = [10.0 + i * 0.1 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, cci_max=-100.0, cci_period=20)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertIn("cci", row)
        self.assertLessEqual(row["cci"], -100.0)

    async def test_donchian_upper_break_detects_new_high(self) -> None:
        # MATCH: calm base then a final gap-up close that clears the prior
        # 20-bar Donchian upper (its highs are below the breakout close).
        match_closes = [20.0] * 399 + [30.0]
        # MISS: same calm base but the last close stays inside the channel.
        miss_closes = [20.0] * 400
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, donchian="upper_break", donchian_window=20)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertIn("donchian_upper", row)
        self.assertIn("donchian_lower", row)

    async def test_keltner_upper_break_detects_breakout(self) -> None:
        # MATCH: calm base then a sharp final jump that pierces the upper
        # Keltner channel. MISS: smooth ramp that stays inside the channel.
        match_closes = [20.0] * 395 + [21.0, 23.0, 26.0, 30.0, 36.0]
        miss_closes = [20.0 + i * 0.01 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, keltner="upper_break")
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertIn("keltner_upper", row)
        self.assertIn("keltner_lower", row)

    async def test_roc_min_matches_strong_momentum(self) -> None:
        # MATCH: steady +0.5/bar climb → high positive ROC; MISS: flat.
        match_closes = [10.0 + i * 0.5 for i in range(400)]
        miss_closes = [10.0] * 400
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=match_closes),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=miss_closes),
            }
        )
        _, payload = await self._run(provider, roc_min=2.0, roc_period=12)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertIn("roc", row)
        self.assertGreaterEqual(row["roc"], 2.0)

    async def test_conflicting_cci_bounds_rejected(self) -> None:
        provider = _FakeProvider({})
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        result = await tool.execute(
            universe=["A.SH"], asof=self.asof.isoformat(), cci_min=100.0, cci_max=-100.0
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_conditions]", result.text)

    async def test_top_k_truncates_and_sort_desc_orders_by_rsi(self) -> None:
        # Three RSI-oversold candidates; verify top-k=2 + sort by rsi desc
        # keeps the two with the largest RSI values among the matches.
        provider = _FakeProvider(
            {
                "A.SH": _make_bars("A.SH", start=self.start, count=400,
                                    closes=[60.0 - i * 0.05 for i in range(400)]),
                "B.SH": _make_bars("B.SH", start=self.start, count=400,
                                    closes=[60.0 - i * 0.1 for i in range(400)]),
                "C.SH": _make_bars("C.SH", start=self.start, count=400,
                                    closes=[60.0 - i * 0.15 for i in range(400)]),
            }
        )
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        result = await tool.execute(
            universe=["A.SH", "B.SH", "C.SH"],
            asof=self.asof.isoformat(),
            rsi_max=50.0,
            top_k=2,
            sort_by="rsi",
            sort_desc=True,
            output_path=str(self.output_path),
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 2)
        rsis = [row["rsi"] for row in payload["preview"]]
        self.assertEqual(rsis, sorted(rsis, reverse=True))


class StockScreenSkipPathTests(unittest.IsolatedAsyncioTestCase):
    """Per-symbol skip paths must each fire a debug event with a distinct reason."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        self.asof = "2026-05-26"

    async def test_no_bars_skip_reason(self) -> None:
        provider = _FakeProvider({})  # symbol has zero bars
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        with patch("doyoutrade.api.operations.stock_screen.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await tool.execute(
                universe=["MISSING.SH"],
                asof=self.asof,
                rsi_max=50.0,
                output_path=str(self.output_path),
            )

        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 0)
        self.assertEqual(payload["skipped"], 1)
        skip_events = [
            call.args for call in emit.await_args_list
            if call.args and call.args[0] == "screener_symbol_skipped"
        ]
        self.assertEqual(len(skip_events), 1)
        _, payload_event = skip_events[0]
        self.assertEqual(payload_event["symbol"], "MISSING.SH")
        self.assertEqual(payload_event["reason"], "no_bars_before_asof")
        self.assertIn("hint", payload_event)

    async def test_insufficient_history_skip_reason(self) -> None:
        # Single bar — too short for any indicator.
        bars = _make_bars("SHORT.SH", start=date(2026, 5, 25), count=1)
        provider = _FakeProvider({"SHORT.SH": bars})
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        with patch("doyoutrade.api.operations.stock_screen.emit_debug_event", new_callable=AsyncMock) as emit:
            await tool.execute(
                universe=["SHORT.SH"],
                asof=self.asof,
                rsi_max=50.0,
                output_path=str(self.output_path),
            )

        skip_events = [
            call.args for call in emit.await_args_list
            if call.args and call.args[0] == "screener_symbol_skipped"
        ]
        self.assertEqual(len(skip_events), 1)
        _, payload_event = skip_events[0]
        self.assertEqual(payload_event["symbol"], "SHORT.SH")
        self.assertEqual(payload_event["reason"], "insufficient_history")

    async def test_bar_fetch_failed_skip_reason_does_not_abort_run(self) -> None:
        # One symbol raises during fetch; the other is fine. The whole run
        # must NOT abort — the broken symbol surfaces as a structured skip.
        good_bars = _make_bars(
            "GOOD.SH", start=date(2025, 1, 1), count=400,
            closes=[50.0 - i * 0.1 for i in range(400)],
        )
        provider = _FakeProvider({"GOOD.SH": good_bars})
        provider.fail_symbols.add("BAD.SH")
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        with patch("doyoutrade.api.operations.stock_screen.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await tool.execute(
                universe=["GOOD.SH", "BAD.SH"],
                asof="2026-01-20",
                rsi_max=50.0,
                output_path=str(self.output_path),
            )

        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["matched"], 1)
        bad_skips = [
            call.args[1] for call in emit.await_args_list
            if call.args and call.args[0] == "screener_symbol_skipped"
            and call.args[1].get("symbol") == "BAD.SH"
        ]
        self.assertEqual(len(bad_skips), 1)
        self.assertEqual(bad_skips[0]["reason"], "bar_fetch_failed")
        self.assertEqual(bad_skips[0]["error_type"], "RuntimeError")


class StockScreenEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    """Verify the structured payload shape consumers depend on."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"

    async def test_payload_contains_expected_keys_and_csv_written(self) -> None:
        bars = _make_bars(
            "MATCH.SH", start=date(2025, 1, 1), count=400,
            closes=[50.0 - i * 0.1 for i in range(400)],
        )
        provider = _FakeProvider({"MATCH.SH": bars})
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        result = await tool.execute(
            universe=["MATCH.SH"],
            asof="2026-02-05",
            rsi_max=50.0,
            output_path=str(self.output_path),
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        for key in (
            "status", "asof", "interval", "data_source", "universe_size",
            "matched", "skipped", "lookback_days", "result_path", "columns",
            "preview",
        ):
            self.assertIn(key, payload, msg=f"payload missing key: {key}")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["asof"], "2026-02-05")
        self.assertEqual(payload["universe_size"], 1)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["skipped"], 0)
        self.assertTrue(payload["result_path"].endswith("result.csv"))
        self.assertTrue(Path(payload["result_path"]).exists())
        # CSV first row covers the matched symbol's "symbol,matched_conditions,close,rsi,..."
        with open(payload["result_path"], encoding="utf-8") as fh:
            header = fh.readline().strip().split(",")
        self.assertIn("symbol", header)
        self.assertIn("matched_conditions", header)
        self.assertIn("rsi", header)

    async def test_provider_aclose_is_called(self) -> None:
        provider = _FakeProvider({})
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        await tool.execute(
            universe=["ANY.SH"],
            asof="2026-05-26",
            rsi_max=50.0,
            output_path=str(self.output_path),
        )
        self.assertTrue(provider.closed)


class StockScreenNewPredicateTests(unittest.IsolatedAsyncioTestCase):
    """M1 atoms: ma_above_ma / ma_slope_min / avg_amount + rank_by ordering."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        self.start = date(2025, 1, 1)
        self.asof = self.start + timedelta(days=399)

    async def _run(self, provider: _FakeProvider, universe: list[str], **kwargs: Any):
        tool = StockScreenTool(data_provider_factory=_factory_from(provider))
        result = await tool.execute(
            universe=universe,
            asof=self.asof.isoformat(),
            output_path=str(self.output_path),
            **kwargs,
        )
        return result

    # --- ma_above_ma -----------------------------------------------------
    async def test_ma_above_ma_matches_short_above_long(self) -> None:
        # MATCH trends up → SMA(20) > SMA(60); MISS trends down → SMA(20) < SMA(60).
        up = [10.0 + i * 0.2 for i in range(400)]
        down = [100.0 - i * 0.2 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=up),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=down),
            }
        )
        result = await self._run(provider, ["MATCH.SH", "MISS.SH"], ma_above_ma="20,60")
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")

    async def test_ma_above_ma_rejects_fast_ge_slow(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, ["A.SH"], ma_above_ma="60,20")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_ma_above_ma]", result.text)

    # --- ma_slope_min ----------------------------------------------------
    async def test_ma_slope_min_matches_rising_ma(self) -> None:
        rising = [10.0 + i * 0.2 for i in range(400)]
        falling = [100.0 - i * 0.2 for i in range(400)]
        provider = _FakeProvider(
            {
                "MATCH.SH": _make_bars("MATCH.SH", start=self.start, count=400, closes=rising),
                "MISS.SH": _make_bars("MISS.SH", start=self.start, count=400, closes=falling),
            }
        )
        result = await self._run(provider, ["MATCH.SH", "MISS.SH"], ma_slope_min="20,5,0")
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "MATCH.SH")
        self.assertIn("ma_slope20", payload["preview"][0])

    async def test_ma_slope_min_rejects_malformed(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, ["A.SH"], ma_slope_min="20,5")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_ma_slope]", result.text)

    # --- avg_amount ------------------------------------------------------
    async def test_avg_amount_matches_high_turnover(self) -> None:
        # MATCH avg turnover over last 10 bars >= 1e9; MISS well below.
        big = _make_bars(
            "MATCH.SH", start=self.start, count=400,
            closes=[10.0] * 400, amounts=[2e9] * 400,
        )
        small = _make_bars(
            "MISS.SH", start=self.start, count=400,
            closes=[10.0] * 400, amounts=[1e8] * 400,
        )
        provider = _FakeProvider({"MATCH.SH": big, "MISS.SH": small})
        result = await self._run(
            provider, ["MATCH.SH", "MISS.SH"],
            avg_amount_lookback=10, avg_amount_min=1e9,
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "MATCH.SH")
        self.assertGreaterEqual(row["avg_amount"], 1e9)

    async def test_avg_amount_skips_symbol_without_turnover(self) -> None:
        # Bars carry no amount (provider didn't supply turnover) → skipped,
        # not silently treated as zero.
        no_amount = _make_bars("X.SH", start=self.start, count=400, closes=[10.0] * 400)
        provider = _FakeProvider({"X.SH": no_amount})
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event",
            new=AsyncMock(),
        ) as mock_emit:
            result = await self._run(
                provider, ["X.SH"], avg_amount_lookback=10, avg_amount_min=1e9,
            )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 0)
        self.assertEqual(payload["skipped"], 1)
        reasons = [
            c.args[1].get("reason")
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == "screener_symbol_skipped"
        ]
        self.assertIn("insufficient_history", reasons)

    async def test_avg_amount_lookback_without_min_rejected(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, ["A.SH"], avg_amount_lookback=10)
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_condition_value]", result.text)

    # --- rank_by ---------------------------------------------------------
    async def test_rank_by_rsi_orders_strongest_first_without_filter(self) -> None:
        # No RSI filter — rank a universe by RSI strength, strongest first.
        provider = _FakeProvider(
            {
                "HI.SH": _make_bars("HI.SH", start=self.start, count=400,
                                    closes=[10.0 + i * 0.5 for i in range(400)]),
                "LO.SH": _make_bars("LO.SH", start=self.start, count=400,
                                    closes=[100.0 - i * 0.2 for i in range(400)]),
            }
        )
        result = await self._run(provider, ["HI.SH", "LO.SH"], rank_by="rsi")
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        # Both match (no filter); ordered by rsi desc → uptrend first.
        self.assertEqual(payload["matched"], 2)
        self.assertEqual(payload["preview"][0]["symbol"], "HI.SH")
        rsis = [r["rsi"] for r in payload["preview"]]
        self.assertEqual(rsis, sorted(rsis, reverse=True))

    async def test_rank_by_respects_top_k(self) -> None:
        # A = clear uptrend (RSI saturates high); B / C trend down at
        # different slopes so RSI strictly orders A > B > C.
        provider = _FakeProvider(
            {
                "A.SH": _make_bars("A.SH", start=self.start, count=400,
                                   closes=[10.0 + i * 0.5 for i in range(400)]),
                "B.SH": _make_bars("B.SH", start=self.start, count=400,
                                   closes=[100.0 - i * 0.1 for i in range(400)]),
                "C.SH": _make_bars("C.SH", start=self.start, count=400,
                                   closes=[100.0 - i * 0.2 for i in range(400)]),
            }
        )
        result = await self._run(
            provider, ["A.SH", "B.SH", "C.SH"], rank_by="rsi", top_k=1,
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "A.SH")

    async def test_rank_order_asc_flips_direction(self) -> None:
        provider = _FakeProvider(
            {
                "HI.SH": _make_bars("HI.SH", start=self.start, count=400,
                                    closes=[10.0 + i * 0.5 for i in range(400)]),
                "LO.SH": _make_bars("LO.SH", start=self.start, count=400,
                                    closes=[100.0 - i * 0.2 for i in range(400)]),
            }
        )
        result = await self._run(provider, ["HI.SH", "LO.SH"], rank_by="rsi", rank_order="asc")
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["preview"][0]["symbol"], "LO.SH")

    async def test_rank_by_avg_amount_requires_lookback(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, ["A.SH"], rank_by="avg_amount")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_rank_metric]", result.text)

    async def test_rank_by_unknown_metric_rejected(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, ["A.SH"], rank_by="sharpe")
        self.assertTrue(result.is_error)
        # _parse_enum raises invalid_condition_value for unknown enum members.
        self.assertTrue(result.is_error)


class StockScreenFundamentalsTests(unittest.IsolatedAsyncioTestCase):
    """M2: --min-float-mv pulls the fundamentals axis into the screener."""

    def setUp(self) -> None:
        from doyoutrade.core.models import Fundamentals

        self.Fundamentals = Fundamentals
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        self.start = date(2025, 1, 1)
        self.asof = self.start + timedelta(days=399)

    def _fund_factory(self, fmap):
        class _Fake:
            from doyoutrade.data.protocols import ProviderCapabilities as _C
            capabilities = _C(name="akshare", supported_intervals=frozenset())

            async def get_fundamentals_batch(self, symbols, *, asof=None):
                return {s: fmap[s] for s in symbols if s in fmap}

            async def get_fundamentals(self, symbol, *, asof=None):
                return fmap.get(symbol)

        return lambda ds: _Fake()

    async def _run(self, provider, fmap, **kwargs):
        tool = StockScreenTool(
            data_provider_factory=_factory_from(provider),
            fundamentals_provider_factory=self._fund_factory(fmap),
        )
        return await tool.execute(
            universe=["BIG.SH", "SMALL.SH"],
            asof=self.asof.isoformat(),
            output_path=str(self.output_path),
            **kwargs,
        )

    async def test_min_float_mv_filters_by_market_cap(self) -> None:
        provider = _FakeProvider({
            "BIG.SH": _make_bars("BIG.SH", start=self.start, count=400, closes=[10.0] * 400),
            "SMALL.SH": _make_bars("SMALL.SH", start=self.start, count=400, closes=[10.0] * 400),
        })
        fmap = {
            "BIG.SH": self.Fundamentals(code="BIG.SH", float_mv=2e10, provider="akshare"),
            "SMALL.SH": self.Fundamentals(code="SMALL.SH", float_mv=5e9, provider="akshare"),
        }
        result = await self._run(provider, fmap, min_float_mv=1e10)
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "BIG.SH")
        self.assertGreaterEqual(payload["preview"][0]["float_mv"], 1e10)

    async def test_missing_fundamentals_skips_with_distinct_reason(self) -> None:
        provider = _FakeProvider({
            "BIG.SH": _make_bars("BIG.SH", start=self.start, count=400, closes=[10.0] * 400),
            "SMALL.SH": _make_bars("SMALL.SH", start=self.start, count=400, closes=[10.0] * 400),
        })
        # SMALL has no fundamentals entry → should skip, not silently pass.
        fmap = {"BIG.SH": self.Fundamentals(code="BIG.SH", float_mv=2e10, provider="akshare")}
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event", new=AsyncMock()
        ) as mock_emit:
            result = await self._run(provider, fmap, min_float_mv=1e10)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["skipped"], 1)
        reasons = [
            c.args[1].get("reason")
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == "screener_symbol_skipped"
        ]
        self.assertIn("fundamentals_unavailable", reasons)

    async def test_conflicting_float_mv_bounds_rejected(self) -> None:
        provider = _FakeProvider({})
        result = await self._run(provider, {}, min_float_mv=2e10, max_float_mv=1e10)
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_conditions]", result.text)

    async def test_fundamentals_fetch_failure_surfaces_error_code(self) -> None:
        provider = _FakeProvider({
            "BIG.SH": _make_bars("BIG.SH", start=self.start, count=400, closes=[10.0] * 400),
        })

        class _Boom:
            from doyoutrade.data.protocols import ProviderCapabilities as _C
            capabilities = _C(name="akshare", supported_intervals=frozenset())

            async def get_fundamentals_batch(self, symbols, *, asof=None):
                raise RuntimeError("snapshot down")

        tool = StockScreenTool(
            data_provider_factory=_factory_from(provider),
            fundamentals_provider_factory=lambda ds: _Boom(),
        )
        result = await tool.execute(
            universe=["BIG.SH"], asof=self.asof.isoformat(),
            min_float_mv=1e10, output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:fundamentals_fetch_failed]", result.text)


class StockScreenEventTests(unittest.IsolatedAsyncioTestCase):
    """M4: --exclude-suspended pulls the event axis and drops halted symbols."""

    def setUp(self) -> None:
        from doyoutrade.core.models import EventItem

        self.EventItem = EventItem
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        self.start = date(2025, 1, 1)
        self.asof = self.start + timedelta(days=399)

    def _ev_factory(self, suspended, *, raise_=False):
        EventItem = self.EventItem

        class _Fake:
            from doyoutrade.data.protocols import ProviderCapabilities as _C
            capabilities = _C(name="akshare", supported_intervals=frozenset())

            async def get_events_batch(self, symbols, *, asof=None):
                if raise_:
                    raise RuntimeError("tfp down")
                return {
                    s: [EventItem(code=s, event_type="suspension", event_date=asof or "",
                                  detail="停牌", provider="akshare")]
                    for s in symbols if s in suspended
                }

        return lambda ds: _Fake()

    async def test_exclude_suspended_drops_halted(self) -> None:
        provider = _FakeProvider({
            "OK.SH": _make_bars("OK.SH", start=self.start, count=400, closes=[10.0] * 400),
            "SUSP.SH": _make_bars("SUSP.SH", start=self.start, count=400, closes=[10.0] * 400),
        })
        tool = StockScreenTool(
            data_provider_factory=_factory_from(provider),
            event_provider_factory=self._ev_factory({"SUSP.SH"}),
        )
        result = await tool.execute(
            universe=["OK.SH", "SUSP.SH"], asof=self.asof.isoformat(),
            exclude_suspended=True, output_path=str(self.output_path),
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["preview"][0]["symbol"], "OK.SH")

    async def test_events_fetch_failure_surfaces_error_code(self) -> None:
        provider = _FakeProvider({
            "OK.SH": _make_bars("OK.SH", start=self.start, count=400, closes=[10.0] * 400),
        })
        tool = StockScreenTool(
            data_provider_factory=_factory_from(provider),
            event_provider_factory=self._ev_factory(set(), raise_=True),
        )
        result = await tool.execute(
            universe=["OK.SH"], asof=self.asof.isoformat(),
            exclude_suspended=True, output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:events_fetch_failed]", result.text)


_VALID_SCORER = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal, indicators as ind


class Strategy(Strategy):
    name = "above_ma_screen"
    timeframe = "1d"
    startup_history = 20

    def on_bar(self, df, ctx) -> Signal:
        ma = ind.sma(df["close"], 20).iloc[-1]
        last = float(df["close"].iloc[-1])
        if last > float(ma):
            return Signal.buy(tag="above_ma20", diagnostics={"gap": last - float(ma)})
        return Signal.hold(tag="below_ma20")
"""

_BAD_SCORER = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    name = "no_on_bar"
    timeframe = "1d"
    startup_history = 5
    # missing on_bar → compile failure
"""


class StockScreenCodeModeTests(unittest.IsolatedAsyncioTestCase):
    """M5: --scorer-file / --by-strategy evaluate a compiled Strategy."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.output_path = Path(self.tmpdir.name) / "result.csv"
        self.start = date(2025, 1, 1)
        self.asof = self.start + timedelta(days=70)
        self.scorer = Path(self.tmpdir.name) / "scorer.py"
        self.scorer.write_text(_VALID_SCORER, encoding="utf-8")

    def _provider(self):
        up = [10.0 + 0.5 * i for i in range(60)]
        down = [60.0 - 0.3 * i for i in range(60)]
        return _FakeProvider({
            "UP.SH": _make_bars("UP.SH", start=self.start, count=60, closes=up),
            "DN.SH": _make_bars("DN.SH", start=self.start, count=60, closes=down),
        })

    async def test_scorer_file_matches_buy_signals(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH", "DN.SH"], asof=self.asof.isoformat(),
            scorer_file=str(self.scorer), rank_by_diagnostic="gap",
            output_path=str(self.output_path),
        )
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["mode"], "code")
        self.assertEqual(payload["matched"], 1)
        row = payload["preview"][0]
        self.assertEqual(row["symbol"], "UP.SH")
        self.assertEqual(row["direction"], "buy")
        self.assertIn("diag.gap", row)

    async def test_signal_direction_any_keeps_all(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH", "DN.SH"], asof=self.asof.isoformat(),
            scorer_file=str(self.scorer), signal_direction="any",
            output_path=str(self.output_path),
        )
        payload = _extract_payload(result)
        self.assertEqual(payload["matched"], 2)

    async def test_compile_failure_surfaces_error(self) -> None:
        bad = Path(self.tmpdir.name) / "bad.py"
        bad.write_text(_BAD_SCORER, encoding="utf-8")
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH"], asof=self.asof.isoformat(),
            scorer_file=str(bad), output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        # compile error_code mirrors sdk validate (missing_on_bar / compile_failed).
        self.assertIn("[error:", result.text)

    async def test_scorer_plus_predicate_is_conflict(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH"], asof=self.asof.isoformat(),
            scorer_file=str(self.scorer), rsi_max=30.0,
            output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_screen_mode]", result.text)

    async def test_scorer_and_by_strategy_conflict(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH"], asof=self.asof.isoformat(),
            scorer_file=str(self.scorer), by_strategy="sd-x",
            output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_screen_mode]", result.text)

    async def test_by_strategy_without_repo_degrades(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH"], asof=self.asof.isoformat(),
            by_strategy="sd-x", output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:by_strategy_unavailable]", result.text)

    async def test_scorer_file_not_found(self) -> None:
        tool = StockScreenTool(data_provider_factory=_factory_from(self._provider()))
        result = await tool.execute(
            universe=["UP.SH"], asof=self.asof.isoformat(),
            scorer_file="/nonexistent/scorer.py", output_path=str(self.output_path),
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:scorer_file_not_found]", result.text)


class _FakeWarehouseRepo:
    """Stand-in for the local ``market_bars`` repository.

    ``bars_in_range`` returns the warehouse rows configured for a symbol (empty =
    a local miss), or raises ``raise_for`` to exercise the read-failure fallback.
    """

    def __init__(
        self,
        rows_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self._rows = dict(rows_by_symbol or {})
        self._raise_for = set(raise_for or set())
        self.calls: list[dict[str, Any]] = []

    async def bars_in_range(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if kwargs["symbol"] in self._raise_for:
            raise RuntimeError(f"warehouse read boom for {kwargs['symbol']}")
        return list(self._rows.get(kwargs["symbol"], []))


def _warehouse_row(symbol: str, timestamp: str, *, close: float = 10.0) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
        "adjust_type": "qfq",
    }


class LocalFirstScreenProviderTests(unittest.IsolatedAsyncioTestCase):
    """Zero-regression read-through: warehouse hit serves locally, miss / read
    failure / unsupported interval all fall back to the exact pre-cache upstream
    call (no ``adjust`` kwarg)."""

    def _provider(self, repo: Any, upstream: Any) -> _LocalFirstScreenProvider:
        return _LocalFirstScreenProvider(
            repository=repo, upstream=upstream, provider="auto", adjust="qfq"
        )

    async def test_warehouse_hit_serves_locally_without_upstream(self) -> None:
        repo = _FakeWarehouseRepo(
            {"600519.SH": [_warehouse_row("600519.SH", "2026-06-18", close=1700.0)]}
        )
        upstream = _FakeProvider({})
        prov = self._provider(repo, upstream)
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event",
            new_callable=AsyncMock,
        ):
            bars = await prov.get_bars("600519.SH", "2026-06-01", "2026-06-20", interval="1d")
        self.assertEqual(len(bars), 1)
        self.assertIsInstance(bars[0], Bar)
        self.assertEqual(bars[0].close, 1700.0)
        self.assertEqual(bars[0].amount, 1700.0 * 1000.0)
        # warehouse hit ⇒ no network round-trip
        self.assertEqual(upstream.calls, [])
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(repo.calls[0]["provider"], "auto")
        self.assertEqual(repo.calls[0]["adjust"], "qfq")

    async def test_warehouse_miss_falls_back_to_upstream(self) -> None:
        repo = _FakeWarehouseRepo({})  # empty warehouse ⇒ miss
        upstream = _FakeProvider(
            {"000001.SZ": _make_bars("000001.SZ", start=date(2026, 6, 1), count=5)}
        )
        prov = self._provider(repo, upstream)
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event",
            new_callable=AsyncMock,
        ):
            bars = await prov.get_bars("000001.SZ", "2026-06-01", "2026-06-20", interval="1d")
        self.assertEqual(len(bars), 5)
        self.assertEqual(len(upstream.calls), 1)  # fell back to the network

    async def test_warehouse_read_failure_falls_back_to_upstream(self) -> None:
        repo = _FakeWarehouseRepo(raise_for={"000001.SZ"})
        upstream = _FakeProvider(
            {"000001.SZ": _make_bars("000001.SZ", start=date(2026, 6, 1), count=3)}
        )
        prov = self._provider(repo, upstream)
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event",
            new_callable=AsyncMock,
        ):
            bars = await prov.get_bars("000001.SZ", "2026-06-01", "2026-06-20", interval="1d")
        self.assertEqual(len(bars), 3)
        self.assertEqual(len(upstream.calls), 1)

    async def test_unsupported_local_interval_bypasses_warehouse(self) -> None:
        repo = _FakeWarehouseRepo({"X.SH": [_warehouse_row("X.SH", "2026-06-18")]})
        upstream = _FakeProvider(
            {"X.SH": _make_bars("X.SH", start=date(2026, 6, 1), count=2)}
        )
        prov = self._provider(repo, upstream)
        with patch(
            "doyoutrade.api.operations.stock_screen.emit_debug_event",
            new_callable=AsyncMock,
        ):
            bars = await prov.get_bars("X.SH", "2026-06-01", "2026-06-20", interval="1w")
        # "1w" is not a local interval ⇒ never touch the warehouse, go upstream.
        self.assertEqual(repo.calls, [])
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(len(bars), 2)

    async def test_aclose_closes_upstream(self) -> None:
        upstream = _FakeProvider({})
        prov = self._provider(_FakeWarehouseRepo({}), upstream)
        await prov.aclose()
        self.assertTrue(upstream.closed)


if __name__ == "__main__":
    unittest.main()
