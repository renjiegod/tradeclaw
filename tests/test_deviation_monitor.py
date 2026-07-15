"""``DeviationMonitorExecutor`` — the 交易纪律提醒 cron task executor.

Covers validate_params, statement parsing helpers, the deterministic reminder
composition, and the full fire pipeline against the REAL
``DeviationGuardStrategy`` rule: a breach (delivered reminder recalling the
thesis), a clean fire (``[SILENT]`` suppression), and the structured skips
(position closed / quote unavailable) plus the strategy-load / statement
failure modes. Delivery is patched so the test needs no channel wiring.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest import mock

import pandas as pd

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.assistant.cron_executors.base import JobRunContext
from doyoutrade.assistant.cron_executors.deviation_monitor import (
    DeviationMonitorExecutor,
    LoadedStrategy,
    _Deviation,
    _Holding,
    compose_reminder,
    parse_account_view,
    parse_holdings,
)
from doyoutrade.strategy_sdk.examples.deviation_guard import DeviationGuardStrategy
from doyoutrade.strategy_sdk.signal import Signal

_FIRED_AT = datetime(2026, 6, 17, 6, 50, tzinfo=timezone.utc)  # 14:50 Asia/Shanghai
_CTX = JobRunContext(cron_job_run_id="crun-1", job_id="task-1", fired_at=_FIRED_AT)
_SYMBOL = "600519.SH"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _history(n: int = 35, last_day: str = "2026-06-13") -> pd.DataFrame:
    # DeviationGuardStrategy.startup_history=30; supply enough daily bars or the
    # runner returns a graceful Signal.hold(tag="data_insufficient").
    idx = pd.date_range(end=last_day, periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


class _FakeFetcher:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def fetch(self, symbol, *, as_of, lookback, freq="1d") -> pd.DataFrame:
        return self._df.copy()


class _CronRepo:
    async def get_job(self, jid):
        return {"id": jid, "name": "纪律提醒-茅台"}


def _statement(held: bool = True) -> dict[str, Any]:
    positions = []
    if held:
        positions = [
            {
                "symbol": _SYMBOL,
                "name": "贵州茅台",
                "quantity": 100,
                "available": 100,
                "cost_price": "95",
                "last_price": "100",
                "market_value": "10000",
                "frozen": 0,
            }
        ]
    return {
        "asof": "2026-06-17",
        "source": "broker",
        "account": {
            "account": {"cash": "1000", "equity": "100000"},
            "positions": positions,
        },
        "asset": None,
        "trades": [],
        "trade_count": 0,
        "errors": [],
    }


def _breach_quote() -> QuoteSnapshot:
    # Big down move below MA5 and below cost (95): break_ma + bearish + vol + below_cost.
    return QuoteSnapshot(
        symbol=_SYMBOL, price=90.0, prev_close=100.0, open=99.0,
        high=99.5, low=88.0, volume=5000.0, status="ok",
    )


def _clean_quote() -> QuoteSnapshot:
    # Mild up day above MA5 and above cost — no rule trips.
    return QuoteSnapshot(
        symbol=_SYMBOL, price=101.0, prev_close=100.0, open=100.0,
        high=101.5, low=99.8, volume=1000.0, status="ok",
    )


async def _loader_ok(sd_id: str) -> LoadedStrategy:
    return LoadedStrategy(
        strategy_class=DeviationGuardStrategy, class_name="DeviationGuardStrategy"
    )


async def _loader_raises(sd_id: str) -> LoadedStrategy:
    raise RuntimeError("compile_failed: bad source")


def _make_executor(
    *,
    loader=_loader_ok,
    statement: dict[str, Any] | None = None,
    statement_raises: bool = False,
    quotes: dict[str, QuoteSnapshot] | None = None,
    history: pd.DataFrame | None = None,
) -> DeviationMonitorExecutor:
    stmt = statement if statement is not None else _statement(held=True)
    qmap = quotes if quotes is not None else {_SYMBOL: _breach_quote()}
    hist = history if history is not None else _history()

    async def _stmt(account_id, asof, captured_at):
        if statement_raises:
            raise RuntimeError("no default account")
        return stmt

    async def _quotes(symbols):
        return {s: qmap.get(s, QuoteSnapshot(symbol=s, status="no_data")) for s in symbols}

    async def _factory(symbols, data_source):
        return _FakeFetcher(hist)

    return DeviationMonitorExecutor(
        assistant_service=object(),
        cron_job_repository=_CronRepo(),
        strategy_loader=loader,
        statement_provider=_stmt,
        quote_fetcher=_quotes,
        history_fetcher_factory=_factory,
    )


def _patch_deliver(status: str = "delivered"):
    async def _fake(svc, *, target_session_id, content, cron_job_id,
                    cron_job_run_id, cron_task_kind):
        _fake.content = content
        _fake.target = target_session_id
        return (status, {})

    return mock.patch(
        "doyoutrade.assistant.cron_executors.deviation_monitor."
        "deliver_assistant_message_to_session",
        _fake,
    ), _fake


def _params(**overrides) -> dict[str, Any]:
    base = {
        "strategy_definition_id": "sd-1",
        "symbols": [_SYMBOL],
        "target_session_id": "asst-sess-1",
        "thesis": "连阳、不破5日线，跌破止损就提醒我",
    }
    base.update(overrides)
    return base


# ── validate_params ──────────────────────────────────────────────────────────


class ValidateParamsTests(unittest.TestCase):
    def setUp(self):
        self.ex = _make_executor()

    def test_ok(self):
        self.assertIsNone(self.ex.validate_params(_params()))

    def test_missing_sd_id(self):
        self.assertEqual(
            self.ex.validate_params({"symbols": [_SYMBOL]})["error_code"],
            "missing_strategy_definition_id",
        )

    def test_missing_symbols(self):
        self.assertEqual(
            self.ex.validate_params({"strategy_definition_id": "sd-1"})["error_code"],
            "missing_symbols",
        )

    def test_invalid_symbols(self):
        err = self.ex.validate_params(
            {"strategy_definition_id": "sd-1", "symbols": ["", 3]}
        )
        self.assertEqual(err["error_code"], "invalid_symbols")

    def test_invalid_thesis(self):
        err = self.ex.validate_params(_params(thesis=123))
        self.assertEqual(err["error_code"], "invalid_thesis")

    def test_invalid_require_position(self):
        err = self.ex.validate_params(_params(require_position="yes"))
        self.assertEqual(err["error_code"], "invalid_require_position")

    def test_non_dict(self):
        self.assertEqual(self.ex.validate_params([])["error_code"], "invalid_task_params")


# ── statement parsing ────────────────────────────────────────────────────────


class StatementParsingTests(unittest.TestCase):
    def test_parse_holdings(self):
        held = parse_holdings(_statement(held=True))
        self.assertIn(_SYMBOL, held)
        self.assertEqual(held[_SYMBOL].cost_price, Decimal("95"))
        self.assertEqual(held[_SYMBOL].quantity, 100.0)
        self.assertEqual(held[_SYMBOL].name, "贵州茅台")

    def test_parse_holdings_empty(self):
        self.assertEqual(parse_holdings(_statement(held=False)), {})

    def test_parse_account_view(self):
        av = parse_account_view(_statement())
        self.assertEqual(av.cash, Decimal("1000"))
        self.assertEqual(av.equity, Decimal("100000"))

    def test_parse_account_view_synthetic_fallback(self):
        av = parse_account_view({"account": None})
        self.assertGreater(av.equity, Decimal("0"))


# ── compose_reminder ─────────────────────────────────────────────────────────


class ComposeReminderTests(unittest.TestCase):
    def test_empty_is_silent(self):
        from doyoutrade.assistant.cron_executors._deliver import SILENT_SENTINEL

        self.assertEqual(compose_reminder(asof_local=_FIRED_AT, deviations=[]), SILENT_SENTINEL)

    def test_recalls_thesis_and_nudges(self):
        dev = _Deviation(
            symbol=_SYMBOL,
            name="贵州茅台",
            signal=Signal.sell(
                tag="break_ma+below_cost",
                rationale="已跌破均线、跌破买入成本",
                diagnostics={"close": 90.0, "ma": 98.0},
            ),
            holding=_Holding(_SYMBOL, 100.0, Decimal("95"), 90.0, "贵州茅台"),
            quote=_breach_quote(),
            thesis="连阳、不破5日线",
        )
        msg = compose_reminder(asof_local=_FIRED_AT, deviations=[dev])
        self.assertIn("连阳、不破5日线", msg)        # thesis recalled
        self.assertIn("已跌破均线", msg)             # deviation explained
        self.assertIn("不及预期", msg)               # nudge
        self.assertIn(_SYMBOL, msg)


# ── full fire pipeline ───────────────────────────────────────────────────────


class RunPipelineTests(unittest.TestCase):
    def test_breach_delivers_reminder(self):
        ex = _make_executor()  # held + breach quote
        patch, fake = _patch_deliver("delivered")
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.delivery_status, "delivered")
        self.assertEqual(res.data["deviation_count"], 1)
        self.assertIn(_SYMBOL, res.data["deviation_symbols"])
        # The pushed content recalls the thesis and nudges to plan.
        self.assertIn("连阳、不破5日线，跌破止损就提醒我", fake.content)
        self.assertIn("不及预期", fake.content)
        self.assertEqual(fake.target, "asst-sess-1")

    def test_clean_fire_is_silent(self):
        ex = _make_executor(quotes={_SYMBOL: _clean_quote()})
        patch, fake = _patch_deliver("suppressed")
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.delivery_status, "suppressed")
        self.assertEqual(res.data["deviation_count"], 0)
        from doyoutrade.assistant.cron_executors._deliver import SILENT_SENTINEL

        self.assertEqual(fake.content, SILENT_SENTINEL)

    def test_position_closed_skips(self):
        ex = _make_executor(statement=_statement(held=False))
        patch, fake = _patch_deliver("suppressed")
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.data["deviation_count"], 0)
        self.assertEqual(res.data["evaluated"], 0)
        reasons = {s["reason"] for s in res.data["skipped"]}
        self.assertIn("position_closed", reasons)

    def test_quote_unavailable_skips(self):
        bad = {_SYMBOL: QuoteSnapshot(symbol=_SYMBOL, status="qmt_disconnected")}
        ex = _make_executor(quotes=bad)
        patch, fake = _patch_deliver("suppressed")
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.data["evaluated"], 0)
        reasons = {s["reason"] for s in res.data["skipped"]}
        self.assertTrue(any("quote_" in r for r in reasons))

    def test_require_position_false_evaluates_unheld(self):
        # No position, but require_position=False → still evaluated (cost rule
        # simply won't fire since there's no cost basis).
        ex = _make_executor(
            statement=_statement(held=False), quotes={_SYMBOL: _breach_quote()}
        )
        patch, fake = _patch_deliver("delivered")
        with patch:
            res = _run(ex.run(_params(require_position=False), _CTX))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.data["evaluated"], 1)
        # break_ma + bearish + volume still trip even without a position.
        self.assertEqual(res.data["deviation_count"], 1)

    def test_strategy_load_failure(self):
        ex = _make_executor(loader=_loader_raises)
        patch, fake = _patch_deliver()
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "failed")
        self.assertIn("strategy_unavailable", res.error)

    def test_statement_failure(self):
        ex = _make_executor(statement_raises=True)
        patch, fake = _patch_deliver()
        with patch:
            res = _run(ex.run(_params(), _CTX))
        self.assertEqual(res.status, "failed")
        self.assertIn("data_unavailable", res.error)


if __name__ == "__main__":
    unittest.main()
