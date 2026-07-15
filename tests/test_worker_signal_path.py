"""End-to-end tests for the new signal-generator path in :class:`TradingWorker`.

Wires up a real :class:`StrategyRunner` (Strategy + PositionManager +
HistoryFetcher) into the worker and verifies one full ``run_cycle``
produces the expected ``OrderIntent`` rows, persists ``position_intents``
into ``cycle_runs.details``, and the new tag flows onto OrderIntent.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pandas as pd

from doyoutrade.account.protocol import AccountReader
from doyoutrade.core.models import (
    AccountSnapshot,
    Bar,
    FillRecord,
    MarketContext,
    OrderIntent,
    PositionSnapshot,
    RiskDecision,
)
from doyoutrade.core.worker import TradingWorker
from doyoutrade.execution.protection import protection_engine_from_config
from doyoutrade.execution.position_manager import (
    PositionConstraints,
    PositionManager,
)
from doyoutrade.runtime.cycle_task import CycleTask, CycleTaskConfig
from doyoutrade.strategy_sdk import Signal, Strategy
from doyoutrade.strategy_sdk.history_fetcher import BarsHistoryFetcher
from doyoutrade.strategy_sdk.runner import StrategyRunner


# ---------- test doubles ----------


def _make_constant_strategy(mapping: dict[str, int]) -> Strategy:
    """Build a Strategy that maps each symbol's target_state (0/1) onto
    Signal.buy/sell/hold based on current position.

    Centralized so each cycle-test test case can pass its own ``mapping``
    without redefining a class.
    """

    class _ConstantStrategy(Strategy):
        timeframe = "1d"
        startup_history = 3

        def on_bar(self, df, ctx):
            target = mapping.get(ctx.symbol)
            if target is None:
                return Signal.hold()
            if target == 1 and not ctx.position.is_long:
                return Signal.buy(tag="constant_long")
            if target == 0 and ctx.position.is_long:
                return Signal.sell(tag="constant_flat")
            return Signal.hold()

    return _ConstantStrategy()


class _MarketProvider:
    def __init__(self, prices: dict[str, float], bars_per_symbol: dict[str, list[Bar]]):
        self._prices = prices
        self._bars = bars_per_symbol

    async def get_market_context(self):
        return MarketContext(
            symbol_to_price=dict(self._prices),
            symbol_to_tick={
                sym: {"close": price} for sym, price in self._prices.items()
            },
        )

    async def get_bars(self, symbol, start_time, end_time, *, interval="1d", adjust="qfq", **_kwargs):
        # ``adjust`` (and any future kwargs) accepted to match the real
        # TradingDataProvider.get_bars signature that BarsHistoryFetcher calls.
        return list(self._bars.get(symbol, []))

    async def is_trading_day(self, _date):
        return True

    async def get_trading_dates(self, _start, _end):
        return []


class _Account(AccountReader):
    portfolio_source = "ledger"

    def __init__(self, cash: float, equity: float, positions: list[PositionSnapshot]):
        self._cash = Decimal(str(cash))
        self._equity = Decimal(str(equity))
        self._positions = positions

    async def get_account_snapshot(self):
        return AccountSnapshot(cash=self._cash, equity=self._equity)

    async def get_positions(self):
        return list(self._positions)


class _StaticUniverse:
    def __init__(self, symbols: list[str]):
        self.symbols = list(symbols)

    async def build_universe(self, *_args, cycle_state=None):
        return list(self.symbols)


class _PassRisk:
    def evaluate(
        self,
        intents,
        account_snapshot,
        positions,
        *,
        cycle_state=None,
        settlement_mode="t0",
    ):
        del account_snapshot, positions, cycle_state, settlement_mode
        return [RiskDecision(intent_id=i.intent_id, action="pass") for i in intents]


@dataclass
class _RecordingExecutionAdapter:
    fills: list[FillRecord] = field(default_factory=list)
    submitted: list[OrderIntent] = field(default_factory=list)

    async def submit_intent(self, intent, *, cycle_state=None, market_context=None):
        self.submitted.append(intent)
        price = float(intent.price_reference)
        if intent.action == "buy":
            qty = float(intent.amount) / price if price > 0 else 0
        else:
            qty = float(intent.amount)
        fill = FillRecord(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.action,
            quantity=qty,
            price=price,
        )
        self.fills.append(fill)
        return fill


@dataclass
class _NoneFillAdapter:
    """Always returns ``None`` — mimics ``PaperExecutionAdapter`` rejecting
    a zero-quantity intent. Used to exercise the worker's dispatch-rejected
    diagnostic path."""

    submitted: list[OrderIntent] = field(default_factory=list)

    async def submit_intent(self, intent, *, cycle_state=None, market_context=None):
        self.submitted.append(intent)
        return None


@dataclass
class _VetoRisk:
    reason: str = "max_position_ratio exceeded"

    def evaluate(
        self,
        intents,
        account_snapshot,
        positions,
        *,
        cycle_state=None,
        settlement_mode="t0",
    ):
        del account_snapshot, positions, cycle_state, settlement_mode
        return [
            RiskDecision(intent_id=i.intent_id, action="veto", reason=self.reason)
            for i in intents
        ]


def _ascending_bars(symbol: str, count: int, *, close_base: float = 10.0) -> list[Bar]:
    out: list[Bar] = []
    for i in range(count):
        ts = f"2026-01-{i+1:02d}"
        close = close_base + i  # strictly increasing
        out.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=close - 0.5,
                high=close + 0.5,
                low=close - 1.0,
                close=close,
                volume=100.0,
            )
        )
    return out


# ---------- recording cycle_runs repository ----------


@dataclass
class _RecordingCycleRunRepository:
    started: list[dict[str, Any]] = field(default_factory=list)
    finalized: list[dict[str, Any]] = field(default_factory=list)

    async def create_started(self, **kwargs):
        self.started.append(dict(kwargs))

    async def finalize(self, run_id, *, status, details_patch, **kwargs):
        record: dict[str, Any] = {
            "run_id": run_id,
            "status": status,
            "details_patch": dict(details_patch),
        }
        record.update(kwargs)
        self.finalized.append(record)


class _NoopTradeFillRepo:
    async def insert_fill(self, **kwargs):
        return True

    async def list_for_task(self, *, task_id, source_mode=None):
        del task_id, source_mode
        return []


@dataclass
class _TaskBudgetTradeFillRepo:
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def insert_fill(self, **kwargs):
        return True

    async def list_for_task(self, *, task_id, source_mode=None):
        out = [row for row in self.rows if row.get("task_id") == task_id]
        if source_mode is not None:
            out = [row for row in out if row.get("source_mode") == source_mode]
        return list(out)


# ---------- legacy strategy stub (required even though unused) ----------


class _LegacyUnusedStrategy:
    """Worker requires a strategy attribute; this should never be called when
    ``signal_generator`` is set."""

    async def invoke(self, *_args, **_kwargs):
        raise AssertionError("legacy strategy.invoke must not be called on the new path")


# ---------- the test cases ----------


class WorkerSignalPathTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_cycle_buy_with_signal_generator(self):
        prices = {"600000.SH": 10.0}
        bars = {"600000.SH": _ascending_bars("600000.SH", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=10000.0, equity=10000.0, positions=[])
        universe = _StaticUniverse(["600000.SH"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        position_manager = PositionManager(
            constraints=PositionConstraints(equity_fraction=1.0),
            strategy_tag="test",
        )
        runner = StrategyRunner(
            strategy=_make_constant_strategy({"600000.SH": 1}),
            position_manager=position_manager,
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )

        report = await worker.run_cycle()

        # New-path emitted exactly one buy.
        self.assertEqual(len(execution.submitted), 1)
        intent = execution.submitted[0]
        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.symbol, "600000.SH")
        self.assertEqual(intent.strategy_tag, "test")
        # T = equity * 1.0 = 10000, cash = 10000, price = 10 -> 1000 shares -> 10000 notional
        self.assertEqual(intent.amount, 10000.0)

        # CycleReport reflects the submission.
        self.assertEqual(report.submitted_count, 1)
        self.assertEqual(report.vetoed_count, 0)
        self.assertFalse(report.cycle_failed)

        # Persistence: cycle_runs.details["position_intents"] populated;
        # legacy decisions / decision_execution empty arrays.
        self.assertEqual(len(cycle_repo.finalized), 1)
        details = cycle_repo.finalized[0]["details_patch"]
        self.assertIn("position_intents", details)
        self.assertEqual(len(details["position_intents"]), 1)
        self.assertEqual(details["position_intents"][0]["action"], "buy")
        self.assertNotIn("decisions", details)
        self.assertNotIn("decision_execution", details)
        # New: per-symbol decision factors + market snapshot persisted to
        # cycle_runs.details (feeds the strategy_signal_alert 'full' push).
        self.assertIn("signal_diagnostics", details)
        diag = details["signal_diagnostics"]["600000.SH"]
        self.assertEqual(diag["direction"], "buy")
        self.assertIn("tag", diag)
        self.assertIn("market_snapshot", details)
        snap_entry = details["market_snapshot"]["600000.SH"]
        self.assertEqual(snap_entry["last_price"], 10.0)
        # No last_close in this provider's tick → pct_change must be None,
        # not a fabricated 0 (guards the divide-by-missing-prior-close path).
        self.assertIsNone(snap_entry["pct_change"])

    async def test_task_budget_snapshot_caps_live_buy_by_existing_task_fills(self):
        prices = {"600000.SH": 10.0}
        bars = {"600000.SH": _ascending_bars("600000.SH", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(
            cash=10000.0,
            equity=10000.0,
            positions=[
                PositionSnapshot(
                    symbol="600000.SH",
                    quantity=300,
                    cost_price=Decimal("9.0"),
                    market_price=10.0,
                    market_value=3000.0,
                    available=300.0,
                )
            ],
        )
        universe = _StaticUniverse(["600000.SH"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()
        trade_fill_repo = _TaskBudgetTradeFillRepo(
            rows=[
                {
                    "task_id": "task-budget-1",
                    "symbol": "600000.SH",
                    "side": "buy",
                    "quantity": "300",
                    "price": "10",
                    "source_mode": "live",
                }
            ]
        )

        position_manager = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0,
                max_task_position_amount=5000.0,
            ),
            strategy_tag="test",
        )

        class _TargetQuantityStrategy(Strategy):
            timeframe = "1d"
            startup_history = 3

            def on_bar(self, df, ctx):
                del df, ctx
                return Signal.target_quantity(quantity=500, tag="budget_target_qty")

        runner = StrategyRunner(
            strategy=_TargetQuantityStrategy(),
            position_manager=position_manager,
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=trade_fill_repo,
            signal_generator=runner,
            run_mode="live",
        )
        worker.cycle_task = CycleTask(
            config=CycleTaskConfig(
                name="budget-task",
                mode="live",
                max_task_position_amount=5000.0,
            ),
            worker=worker,
            task_id="task-budget-1",
        )

        report = await worker.run_cycle()

        self.assertEqual(report.submitted_count, 1)
        self.assertEqual(len(execution.submitted), 1)
        # Existing task-owned usage = 300 * 10 = 3000, remaining budget = 2000.
        self.assertEqual(execution.submitted[0].amount, 2000.0)
        details = cycle_repo.finalized[0]["details_patch"]
        self.assertIn("task_budget", details)
        task_budget = details["task_budget"]
        self.assertEqual(task_budget["budget_cap"], "5000")
        self.assertEqual(task_budget["current_usage"], "3000")
        self.assertEqual(task_budget["remaining_budget"], "2000")

    async def test_observability_disabled_skips_cycle_run_persistence(self):
        # Fast (non-debug) backtest mode: cycle_runs are not persisted, but the
        # cycle still runs and submits the order (business logic untouched).
        from doyoutrade.debug.context import observability_disabled

        prices = {"600000.SH": 10.0}
        bars = {"600000.SH": _ascending_bars("600000.SH", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=10000.0, equity=10000.0, positions=[])
        universe = _StaticUniverse(["600000.SH"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        position_manager = PositionManager(
            constraints=PositionConstraints(equity_fraction=1.0),
            strategy_tag="test",
        )
        runner = StrategyRunner(
            strategy=_make_constant_strategy({"600000.SH": 1}),
            position_manager=position_manager,
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )
        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )

        with observability_disabled():
            report = await worker.run_cycle()

        # Order still submitted — fast mode does not change business outcome.
        self.assertEqual(len(execution.submitted), 1)
        self.assertEqual(report.submitted_count, 1)
        self.assertFalse(report.cycle_failed)
        # No cycle_runs rows written.
        self.assertEqual(cycle_repo.started, [])
        self.assertEqual(cycle_repo.finalized, [])

    async def test_full_cycle_sell_with_existing_position(self):
        prices = {"X": 20.0}
        bars = {"X": _ascending_bars("X", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(
            cash=100.0,
            equity=2100.0,
            positions=[
                PositionSnapshot(symbol="X", quantity=100, cost_price=Decimal("18"))
            ],
        )
        universe = _StaticUniverse(["X"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        position_manager = PositionManager()
        runner = StrategyRunner(
            strategy=_make_constant_strategy({"X": 0}),  # exit
            position_manager=position_manager,
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )

        report = await worker.run_cycle()

        self.assertEqual(len(execution.submitted), 1)
        intent = execution.submitted[0]
        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.amount, 100.0)
        self.assertEqual(report.submitted_count, 1)

    async def test_signal_zero_with_no_position_no_intent(self):
        prices = {"X": 10.0}
        bars = {"X": _ascending_bars("X", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=1000.0, equity=1000.0, positions=[])
        universe = _StaticUniverse(["X"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        runner = StrategyRunner(
            strategy=_make_constant_strategy({"X": 0}),
            position_manager=PositionManager(),
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )

        report = await worker.run_cycle()

        self.assertEqual(len(execution.submitted), 0)
        self.assertEqual(report.submitted_count, 0)
        details = cycle_repo.finalized[0]["details_patch"]
        self.assertEqual(details["position_intents"], [])

    async def test_insufficient_history_skips_symbol(self):
        prices = {"Y": 10.0}
        # Only 2 bars but engine requires 3 -> insufficient history -> no signal
        bars = {"Y": _ascending_bars("Y", count=2)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=10000.0, equity=10000.0, positions=[])
        universe = _StaticUniverse(["Y"])
        execution = _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        runner = StrategyRunner(
            strategy=_make_constant_strategy({"Y": 1}),
            position_manager=PositionManager(),
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )

        report = await worker.run_cycle()
        # Engine sees frame of length 2 < required 3, emits nothing
        self.assertEqual(len(execution.submitted), 0)
        self.assertEqual(report.submitted_count, 0)


class WorkerDownstreamDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    """Coverage for the worker's risk / approval / dispatch fail-loud paths."""

    async def _run_cycle_with(
        self,
        *,
        risk_engine,
        execution=None,
        signal=1,
    ):
        prices = {"X": 10.0}
        bars = {"X": _ascending_bars("X", count=10)}
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=10000.0, equity=10000.0, positions=[])
        execution = execution or _RecordingExecutionAdapter()
        cycle_repo = _RecordingCycleRunRepository()

        runner = StrategyRunner(
            strategy=_make_constant_strategy({"X": signal}),
            position_manager=PositionManager(),
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )

        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=_StaticUniverse(["X"]),
            risk_engine=risk_engine,
            execution_adapter=execution,
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
        )
        report = await worker.run_cycle()
        return report, execution, cycle_repo

    async def test_risk_veto_increments_vetoed_count(self):
        """Risk-engine veto must not look like a successful submission."""
        report, execution, _ = await self._run_cycle_with(
            risk_engine=_VetoRisk(reason="max_position_ratio exceeded"),
        )
        # PositionManager built one buy intent; risk vetoed it.
        self.assertEqual(report.submitted_count, 0)
        self.assertEqual(report.vetoed_count, 1)
        # No submit_intent call should reach the execution adapter.
        self.assertEqual(len(execution.submitted), 0)

    async def test_zero_fill_adapter_marks_dispatch_rejected(self):
        """Adapter that returns ``None`` (zero-quantity fill) bumps
        ``vetoed_count`` instead of ``submitted_count`` — caller can tell
        the order did not actually trade."""
        report, execution, _ = await self._run_cycle_with(
            risk_engine=_PassRisk(),
            execution=_NoneFillAdapter(),
        )
        # Adapter saw the intent but produced no fill.
        self.assertEqual(len(execution.submitted), 1)
        self.assertEqual(report.submitted_count, 0)
        self.assertEqual(report.vetoed_count, 1)


class _AssertingExecutionAdapter:
    """Execution adapter that fails the test if it is ever invoked.

    Used by ``signal_only`` mode tests to assert the worker truly
    short-circuits before dispatch.
    """

    async def submit_intent(self, intent, *, cycle_state=None, market_context=None):
        raise AssertionError(
            "execution_adapter.submit_intent must NOT be called in signal_only mode"
        )


class _AssertingRiskEngine:
    """Risk engine that fails the test if it is ever invoked.

    Used by ``signal_only`` mode tests to assert risk evaluation is
    also skipped (the short-circuit happens before validator / risk).
    """

    def evaluate(
        self,
        intents,
        account_snapshot,
        positions,
        *,
        cycle_state=None,
        settlement_mode="t0",
    ):
        del account_snapshot, positions, cycle_state, settlement_mode
        raise AssertionError(
            "risk_engine.evaluate must NOT be called in signal_only mode"
        )


class _AssertingApprovalGate:
    """Approval gate that fails the test if it is ever invoked."""

    def request(self, *_args, **_kwargs):
        raise AssertionError(
            "approval_gate.request must NOT be called in signal_only mode"
        )


class _SpanEventCapture:
    """Capture the span events emitted via ``emit_debug_event`` for a single cycle.

    Uses the same path as production: ``emit_debug_event`` writes events into
    the currently-active OTel span via ``span.add_event(name, {payload_json})``.
    The capture installs an in-process tracer provider and replaces the
    ``doyoutrade.core.worker.tracer`` module attribute so the worker emits
    spans into the in-memory exporter. (Same pattern used by
    ``tests/test_cron_manager.py::AgentCronManagerSpanTests``.)
    """

    def __init__(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        import doyoutrade.core.worker as worker_mod

        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._provider = provider
        self._worker_mod = worker_mod
        self._orig_tracer = worker_mod.tracer
        worker_mod.tracer = provider.get_tracer(worker_mod.__name__)

    def shutdown(self) -> None:
        """Restore the original worker tracer.

        Intentionally does NOT clear the in-memory exporter — the caller
        still needs to inspect captured spans after ``shutdown()`` is
        invoked (typically from a ``finally`` block in the test).
        """
        self._worker_mod.tracer = self._orig_tracer
        # NB: ``self._provider.shutdown()`` is omitted because OTel's
        # SDK does not allow ``set_tracer_provider`` to overwrite the
        # global provider; the in-memory provider is only referenced
        # via the patched module-level ``tracer`` and is discarded
        # when the capture goes out of scope.

    def events_by_name(self, name: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for span in self.exporter.get_finished_spans():
            for ev in span.events or []:
                if ev.name == name:
                    out.append(
                        {
                            "name": ev.name,
                            "span_name": span.name,
                            "span_attrs": dict(span.attributes or {}),
                            "attrs": dict(ev.attributes or {}),
                        }
                    )
        return out


class WorkerSignalOnlyModeTest(unittest.IsolatedAsyncioTestCase):
    """Cover the ``run_mode == 'signal_only'`` short-circuit path.

    Asserts the worker emits intents but skips validator / risk / approval /
    dispatch / sync_fills and lands on ``persist_trace_and_metrics`` with
    zero counters and an empty ``fills`` list.
    """

    async def _build_worker(
        self,
        *,
        prices: dict[str, float],
        bars: dict[str, list[Bar]],
        signal_map: dict[str, int],
        cash: float = 10000.0,
        equity: float = 10000.0,
        positions: list[PositionSnapshot] | None = None,
        execution_adapter=None,
        risk_engine=None,
        approval_gate=None,
    ) -> tuple[TradingWorker, _RecordingCycleRunRepository]:
        data_provider = _MarketProvider(prices, bars)
        account = _Account(cash=cash, equity=equity, positions=positions or [])
        universe = _StaticUniverse(list(prices.keys()))
        cycle_repo = _RecordingCycleRunRepository()
        runner = StrategyRunner(
            strategy=_make_constant_strategy(signal_map),
            position_manager=PositionManager(
                # Modest per-symbol allocation so multiple long signals
                # share the cash budget and each produces its own intent.
                constraints=PositionConstraints(equity_fraction=0.4),
                strategy_tag="signal_only_test",
            ),
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )
        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=universe,
            risk_engine=risk_engine or _AssertingRiskEngine(),
            execution_adapter=execution_adapter or _AssertingExecutionAdapter(),
            cycle_run_repository=cycle_repo,
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
            run_mode="signal_only",
            approval_gate=approval_gate or _AssertingApprovalGate(),
        )
        return worker, cycle_repo

    async def test_signal_only_mode_short_circuits_after_generate_signals(self):
        """signal_only run produces intents but never dispatches.

        Validator / risk / approval / dispatch are wired with raising
        stubs; the short-circuit must keep them untouched. Counters
        stay at 0, ``cycle_failed`` is false, ``details.fills`` is the
        empty list, and ``run_mode`` round-trips as ``signal_only``.
        """
        # Two long signals so the strategy emits more than one intent.
        prices = {"A": 10.0, "B": 20.0}
        bars = {
            "A": _ascending_bars("A", count=10, close_base=10.0),
            "B": _ascending_bars("B", count=10, close_base=20.0),
        }
        worker, cycle_repo = await self._build_worker(
            prices=prices,
            bars=bars,
            signal_map={"A": 1, "B": 1},
        )

        report = await worker.run_cycle()

        # Counters reflect "no dispatch" — not "all vetoed".
        self.assertEqual(report.submitted_count, 0)
        self.assertEqual(report.vetoed_count, 0)
        self.assertEqual(report.pending_approval_count, 0)
        self.assertFalse(report.cycle_failed)
        # Cycle ended on persist_trace_and_metrics; dispatch / sync phases
        # MUST be absent so an operator inspecting the report can tell
        # this was an informational run.
        self.assertIn("generate_signals", report.completed_phases)
        self.assertIn("persist_trace_and_metrics", report.completed_phases)
        for skipped in (
            "run_risk_checks",
            "await_approval_if_needed",
            "dispatch_orders",
            "sync_fills_and_positions",
        ):
            self.assertNotIn(skipped, report.completed_phases)

        # Persistence: position_intents is populated, fills is [],
        # run_mode is signal_only.
        self.assertEqual(len(cycle_repo.started), 1)
        started = cycle_repo.started[0]
        self.assertEqual(started["run_mode"], "signal_only")
        self.assertEqual(len(cycle_repo.finalized), 1)
        finalized = cycle_repo.finalized[0]
        details = finalized["details_patch"]
        self.assertIn("position_intents", details)
        self.assertEqual(len(details["position_intents"]), 2)
        for intent_record in details["position_intents"]:
            self.assertEqual(intent_record["action"], "buy")
        # fills MUST be present (so the API shape stays stable) and empty.
        self.assertIn("fills", details)
        self.assertEqual(details["fills"], [])
        # Counters propagate to the finalize call too.
        self.assertEqual(finalized.get("submitted_count"), 0)
        self.assertEqual(finalized.get("vetoed_count"), 0)
        self.assertEqual(finalized.get("pending_approval_count"), 0)
        self.assertFalse(finalized.get("cycle_failed"))
        self.assertEqual(finalized.get("status"), "completed")

    async def test_signal_only_mode_emits_shortcut_debug_event(self):
        """The new branch must be visible in trace via
        ``worker.phase.signal_only_shortcut`` span event + ``worker.mode``
        attribute on the root cycle span. (§错误可见性: new branch must be
        visible in OTel span + debug event.)
        """
        capture = _SpanEventCapture()
        try:
            prices = {"A": 10.0}
            bars = {"A": _ascending_bars("A", count=10, close_base=10.0)}
            worker, _ = await self._build_worker(
                prices=prices,
                bars=bars,
                signal_map={"A": 1},
            )
            await worker.run_cycle()
        finally:
            capture.shutdown()

        events = capture.events_by_name("worker.phase.signal_only_shortcut")
        self.assertEqual(
            len(events),
            1,
            f"expected exactly one signal_only_shortcut event; got {events!r}",
        )
        # The phase-span name must be the dedicated short-circuit name so
        # debug-UI consumers can filter on it.
        self.assertEqual(events[0]["span_name"], "worker.phase.signal_only_shortcut")
        attrs = events[0]["span_attrs"]
        self.assertEqual(attrs.get("worker.mode"), "signal_only")
        self.assertEqual(attrs.get("doyoutrade.signal_only.intent_count"), 1)

        # The root ``worker.run_cycle`` span also tags ``worker.mode``.
        root_spans = [
            s
            for s in capture.exporter.get_finished_spans()
            if s.name == "worker.run_cycle"
        ]
        self.assertGreaterEqual(len(root_spans), 1)
        self.assertEqual(
            (root_spans[0].attributes or {}).get("worker.mode"), "signal_only"
        )


class MarketSnapshotHelperTest(unittest.TestCase):
    """`_market_snapshot_from_context` — price/pct extraction for the
    no_signal_mode='full' push. pct_change must be derived from last_close and
    must stay None (not 0) when the prior close is missing/zero."""

    def test_pct_change_derived_from_last_close(self):
        from doyoutrade.core.worker import _market_snapshot_from_context

        ctx = MarketContext(
            symbol_to_price={"601138.SH": 82.1},
            symbol_to_tick={
                "601138.SH": {
                    "last_price": 82.1,
                    "last_close": 80.01,
                    "open": 79.95,
                    "high": 84.95,
                    "low": 78.43,
                }
            },
        )
        snap = _market_snapshot_from_context(ctx)
        entry = snap["601138.SH"]
        self.assertEqual(entry["last_price"], 82.1)
        self.assertEqual(entry["prev_close"], 80.01)
        self.assertEqual(entry["pct_change"], 2.61)  # (82.1-80.01)/80.01*100
        self.assertEqual(entry["high"], 84.95)

    def test_pct_change_none_when_prev_close_missing_or_zero(self):
        from doyoutrade.core.worker import _market_snapshot_from_context

        ctx = MarketContext(
            symbol_to_price={"A.SH": 5.0, "B.SH": 9.0},
            symbol_to_tick={
                "A.SH": {"last_price": 5.0},            # no last_close
                "B.SH": {"last_price": 9.0, "last_close": 0},  # zero prior close
            },
        )
        snap = _market_snapshot_from_context(ctx)
        self.assertIsNone(snap["A.SH"]["pct_change"])
        self.assertIsNone(snap["A.SH"]["prev_close"])
        self.assertIsNone(snap["B.SH"]["pct_change"])

    def test_falls_back_to_pre_close_key(self):
        from doyoutrade.core.worker import _market_snapshot_from_context

        ctx = MarketContext(
            symbol_to_price={"C.SH": 11.0},
            symbol_to_tick={"C.SH": {"last_price": 11.0, "pre_close": 10.0}},
        )
        entry = _market_snapshot_from_context(ctx)["C.SH"]
        self.assertEqual(entry["prev_close"], 10.0)
        self.assertEqual(entry["pct_change"], 10.0)


class WorkerProtectionTest(unittest.IsolatedAsyncioTestCase):
    """The portfolio circuit breaker halts new BUYs once a drawdown breaches
    the configured threshold; without a breach it is a no-op."""

    def _build(self, *, protection_engine):
        data_provider = _MarketProvider(
            {"600000.SH": 10.0}, {"600000.SH": _ascending_bars("600000.SH", count=10)}
        )
        account = _Account(cash=10000.0, equity=10000.0, positions=[])
        execution = _RecordingExecutionAdapter()
        runner = StrategyRunner(
            strategy=_make_constant_strategy({"600000.SH": 1}),
            position_manager=PositionManager(
                constraints=PositionConstraints(equity_fraction=1.0), strategy_tag="test"
            ),
            history_fetcher=BarsHistoryFetcher(data_provider=data_provider),
        )
        worker = TradingWorker(
            data_provider=data_provider,
            account_reader=account,
            universe_provider=_StaticUniverse(["600000.SH"]),
            risk_engine=_PassRisk(),
            execution_adapter=execution,
            cycle_run_repository=_RecordingCycleRunRepository(),
            trade_fill_repository=_NoopTradeFillRepo(),
            signal_generator=runner,
            protection_engine=protection_engine,
        )
        return worker, execution

    async def test_protection_halts_buy_on_drawdown(self):
        eng = protection_engine_from_config({"max_drawdown_pct": 0.2})
        eng.evaluate(20000)  # seed a peak; current equity 10000 → 50% dd → halt
        worker, execution = self._build(protection_engine=eng)
        report = await worker.run_cycle()
        self.assertEqual(len(execution.submitted), 0)  # buy halted
        self.assertEqual(report.submitted_count, 0)
        self.assertEqual(report.vetoed_count, 1)
        self.assertFalse(report.cycle_failed)

    async def test_protection_allows_buy_within_threshold(self):
        eng = protection_engine_from_config({"max_drawdown_pct": 0.2})
        eng.evaluate(10500)  # peak 10500 vs equity 10000 → ~4.8% dd → no halt
        worker, execution = self._build(protection_engine=eng)
        report = await worker.run_cycle()
        self.assertEqual(len(execution.submitted), 1)  # buy proceeds
        self.assertEqual(report.submitted_count, 1)
        self.assertEqual(report.vetoed_count, 0)


if __name__ == "__main__":
    unittest.main()
