import asyncio
import tempfile
import unittest
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from doyoutrade.core.models import AccountSnapshot, Bar, CycleReport, PositionSnapshot
from doyoutrade.observability.debug_span_export import ensure_debug_span_export_processors, register_span_persist_sink
from doyoutrade.observability.tracing import configure_tracing
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import (
    SqlAlchemyDebugSessionRepository,
    SqlAlchemyDebugSessionSpanRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyRunRepository,
    SqlAlchemyCycleRunRepository,
    SqlAlchemyTradeFillRepository,
    SqlAlchemyModelRouteRepository,
    SqlAlchemySystemStateRepository,
)
from doyoutrade.platform.service import TradingPlatformService
from doyoutrade.runtime.cycle_task import (
    DEFAULT_REACT_MAX_TURNS,
    DEFAULT_SIGNAL_TOOL_NAMES,
    merge_task_settings,
)
from doyoutrade.runtime.scheduler import RuntimeScheduler
from doyoutrade.persistence.repositories import SqlAlchemyAccountRepository
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp


class _CountingWorker:
    def __init__(self, config=None):
        self.cycles = 0
        self.config = config

    async def run_cycle(self, cycle_persist_context=None):
        self.cycles += 1
        return {
            "submitted_count": 0,
            "vetoed_count": 0,
            "pending_approval_count": 0,
            "completed_phases": ["run_strategy"],
        }


class _FailingWorker:
    def __init__(self, message: str):
        self.cycles = 0
        self._message = message

    async def run_cycle(self, cycle_persist_context=None):
        raise RuntimeError(self._message)


class _FailedSignalCycleWorker:
    """Worker that completes run_cycle but reports signal-phase failure (no exception)."""

    def __init__(self, config=None):
        self.config = config
        self.last_run_id = ""

    async def run_cycle(self, cycle_persist_context=None):
        self.last_run_id = f"run-{uuid.uuid4()}"
        return CycleReport(
            submitted_count=0,
            vetoed_count=0,
            pending_approval_count=0,
            completed_phases=["load_context", "persist_trace_and_metrics"],
            cycle_failed=True,
            failure_message="signal blew up",
            failure_error={"code": "test"},
        )


class _BacktestSummaryWorker:
    """Minimal backtest worker that drives ``_run_backtest_job_body`` to finalize.

    Each cycle bumps the mock ledger cash by 100 so the equity curve has
    variation; an optional ``fail_at_cycle`` makes the N-th cycle return a
    ``cycle_failed`` ``CycleReport`` to exercise the failure finalize path.
    """

    def __init__(self, *, fail_at_cycle: int | None = None, data_provider=None):
        from doyoutrade.account.store_reader import StoreBackedAccountReader
        from doyoutrade.data.mock_provider import MockTradingDataProvider

        self.config = None
        self.last_run_id = ""
        self.cycle_task = None
        self._cycles = 0
        self._fail_at = fail_at_cycle
        self._store = MockTradingDataProvider()
        self.data_provider = data_provider if data_provider is not None else self._store
        self.account_reader = StoreBackedAccountReader(self._store)

    async def run_cycle(self, cycle_persist_context=None):
        self._cycles += 1
        self.last_run_id = f"run-{self._cycles}-{uuid.uuid4()}"
        self._store._cash += Decimal("100")
        if self._fail_at is not None and self._cycles == self._fail_at:
            return CycleReport(
                submitted_count=0,
                vetoed_count=0,
                pending_approval_count=0,
                completed_phases=["test"],
                cycle_failed=True,
                failure_message=f"fail-cycle-{self._cycles}",
            )
        return CycleReport(
            submitted_count=0,
            vetoed_count=0,
            pending_approval_count=0,
            completed_phases=["test"],
            cycle_failed=False,
        )


class _CycleContextRecordingBacktestWorker(_BacktestSummaryWorker):
    """Backtest worker that records every cycle context for interval assertions."""

    def __init__(self, *, data_provider=None):
        super().__init__(data_provider=data_provider)
        self.cycle_contexts: list[dict] = []

    async def run_cycle(self, cycle_persist_context=None):
        self.cycle_contexts.append(dict(cycle_persist_context or {}))
        return await super().run_cycle(cycle_persist_context=cycle_persist_context)


class _RecordingBarsDataProvider:
    def __init__(self, inner):
        self._inner = inner
        self.get_bars_calls: list[tuple[str, str, str, str, str]] = []

    async def get_market_context(self):
        return await self._inner.get_market_context()

    async def get_bars(self, symbol: str, start_time: str, end_time: str, *, interval: str = "1d", adjust: str = "qfq", **_kwargs):
        self.get_bars_calls.append((symbol, start_time, end_time, interval, adjust))
        return await self._inner.get_bars(symbol, start_time, end_time, interval=interval, adjust=adjust)

    async def is_trading_day(self, value: str) -> bool:
        return await self._inner.is_trading_day(value)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return await self._inner.get_trading_dates(start, end)


def _intraday_bar(sym: str, ts: str, close: float) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=normalize_bar_timestamp(ts),
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000.0,
    )


class _FakeTickSessionRepository:
    def __init__(self, debug_session_repo, debug_session_span_repo, task_repository):
        self._session_repo = debug_session_repo
        self._span_repo = debug_session_span_repo
        self._task_repo = task_repository

    async def get_or_create_scheduled_session(self, task_id: str):
        return await self._session_repo.create_session(
            session_id=f"scheduled-{task_id}",
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="scheduled",
        )

    async def create_manual_session(self, task_id: str):
        return await self._session_repo.create_session(
            session_id=f"manual-{uuid.uuid4()}",
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="manual",
        )

    async def create_cron_session(self, task_id: str):
        return await self._session_repo.create_session(
            session_id=f"cron-{uuid.uuid4()}",
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="cron",
        )


class _FakeMarketBarsRepository:
    def __init__(self, *, bars: list[dict] | None = None, sync_state: dict | None = None):
        self._bars = list(bars or [])
        self._sync_state = dict(sync_state) if isinstance(sync_state, dict) else sync_state
        self.bars_in_range_calls: list[dict[str, object]] = []
        self.get_sync_state_calls: list[dict[str, str]] = []
        self.upsert_bars_calls: list[dict[str, object]] = []
        self.mark_sync_success_calls: list[dict[str, object]] = []
        self.mark_sync_failure_calls: list[dict[str, object]] = []

    async def bars_in_range(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        start,
        end,
    ) -> list[dict]:
        self.bars_in_range_calls.append(
            {
                "provider": provider,
                "adjust": adjust,
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
            }
        )
        return list(self._bars)

    async def get_sync_state(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
    ) -> dict | None:
        self.get_sync_state_calls.append(
            {
                "provider": provider,
                "adjust": adjust,
                "symbol": symbol,
                "interval": interval,
            }
        )
        return dict(self._sync_state) if isinstance(self._sync_state, dict) else self._sync_state

    async def upsert_bars(
        self,
        *,
        provider: str,
        adjust: str,
        interval: str,
        bars: list[dict],
    ) -> int:
        self.upsert_bars_calls.append(
            {
                "provider": provider,
                "adjust": adjust,
                "interval": interval,
                "bars": list(bars),
            }
        )
        return len(bars)

    async def mark_sync_success(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        target_start,
        target_end,
        covered_start,
        covered_end,
    ) -> None:
        self.mark_sync_success_calls.append(
            {
                "provider": provider,
                "adjust": adjust,
                "symbol": symbol,
                "interval": interval,
                "target_start": target_start,
                "target_end": target_end,
                "covered_start": covered_start,
                "covered_end": covered_end,
            }
        )
        self._sync_state = {
            "symbol": symbol,
            "interval": interval,
            "provider": provider,
            "adjust": adjust,
            "target_start": target_start.isoformat(),
            "target_end": target_end.isoformat(),
            "covered_start": covered_start.isoformat(),
            "covered_end": covered_end.isoformat(),
            "status": "ok",
        }

    async def mark_sync_failure(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        target_start,
        target_end,
        error_code: str,
        error_type: str,
        error_message: str,
    ) -> None:
        self.mark_sync_failure_calls.append(
            {
                "provider": provider,
                "adjust": adjust,
                "symbol": symbol,
                "interval": interval,
                "target_start": target_start,
                "target_end": target_end,
                "error_code": error_code,
                "error_type": error_type,
                "error_message": error_message,
            }
        )


class _FakeBar:
    def __init__(
        self,
        *,
        symbol: str,
        timestamp: str,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        amount: float | None,
        adjust_type: str = "qfq",
    ):
        self.symbol = symbol
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.amount = amount
        self.adjust_type = adjust_type


class _FakeBarsDataProvider:
    def __init__(self, bars):
        self._bars = list(bars)
        self.calls: list[dict[str, object]] = []

    async def get_bars(self, symbol: str, start_time: str, end_time: str, *, interval: str = "1d", adjust: str = "qfq", **_kwargs):
        self.calls.append(
            {
                "symbol": symbol,
                "start": start_time,
                "end": end_time,
                "interval": interval,
            }
        )
        return list(self._bars)

    async def aclose(self):
        return None


class _ScriptedBarsDataProvider:
    """Returns a different bar list per get_bars call, in order."""

    def __init__(self, responses: list[list]):
        self._responses = [list(items) for items in responses]
        self.calls: list[dict[str, object]] = []

    async def get_bars(self, symbol: str, start_time: str, end_time: str, *, interval: str = "1d", adjust: str = "qfq", **_kwargs):
        self.calls.append(
            {
                "symbol": symbol,
                "start": start_time,
                "end": end_time,
                "interval": interval,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"unexpected extra get_bars call: {symbol} {start_time}..{end_time}"
            )
        return self._responses.pop(0)

    async def aclose(self):
        return None


def _daily_fake_bar(timestamp: str, close: float) -> "_FakeBar":
    return _FakeBar(
        symbol="600000.SH",
        timestamp=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000.0,
        amount=close * 1000.0,
        adjust_type="qfq",
    )


class _FakeRangeSyncRunner:
    def __init__(
        self,
        *,
        result_count: int = 0,
        warnings: list[str] | None = None,
        wait_event: asyncio.Event | None = None,
        exc: Exception | None = None,
    ):
        self.result_count = result_count
        self.warnings = list(warnings or [])
        self.wait_event = wait_event
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    async def sync_range(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str,
        adjust: str,
        mode: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
                "provider": provider,
                "adjust": adjust,
                "mode": mode,
            }
        )
        if self.wait_event is not None:
            await self.wait_event.wait()
        if self.exc is not None:
            raise self.exc
        return {
            "fetched_segments": [{"start": start, "end": end, "status": "fetched"}],
            "upserted_count": self.result_count,
            "warnings": list(self.warnings),
        }


class PlatformServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        configure_tracing(tracing_enabled=True)
        ensure_debug_span_export_processors()
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.task_repository = SqlAlchemyTaskRepository(self.session_factory)
        self.system_state_repository = SqlAlchemySystemStateRepository(self.session_factory)
        self.debug_session_repo = SqlAlchemyDebugSessionRepository(self.session_factory)
        self.debug_session_span_repo = SqlAlchemyDebugSessionSpanRepository(self.session_factory)
        self.run_repository = SqlAlchemyRunRepository(self.session_factory)
        self.cycle_run_repository = SqlAlchemyCycleRunRepository(self.session_factory)
        self.trade_fill_repository = SqlAlchemyTradeFillRepository(self.session_factory)
        self.tick_session_repo = _FakeTickSessionRepository(
            self.debug_session_repo, self.debug_session_span_repo, self.task_repository
        )

        span_repo = self.debug_session_span_repo

        async def _append(row: dict):
            await span_repo.append_span(**row)

        def _sink(row: dict) -> None:
            asyncio.get_running_loop().create_task(_append(row))

        register_span_persist_sink(_sink)

        self.model_route_repo = SqlAlchemyModelRouteRepository(self.session_factory)

        self.account_repo = SqlAlchemyAccountRepository(self.session_factory)

        await self.model_route_repo.create(
            route_name="platform-test-route",
            provider_kind="anthropic",
            api_key="sk-platform-test",
            target_model="gpt-4o-mini",
        )
        self._default_model_route = "platform-test-route"

    def _inst(self, extra: dict | None = None) -> dict:
        base: dict = {"model_route_name": self._default_model_route}
        if extra:
            base = {**base, **extra}
        return merge_task_settings(base)

    async def asyncTearDown(self):
        register_span_persist_sink(None)
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _build_service(self, worker_factory=None):
        from doyoutrade.config import get_config

        return TradingPlatformService(
            scheduler=RuntimeScheduler(),
            app_cfg=get_config(),
            worker_factory=worker_factory or (lambda config, ms, acct=None: _CountingWorker(config)),
            task_repository=self.task_repository,
            account_repository=self.account_repo,
            system_state_repository=self.system_state_repository,
            debug_session_repository=self.debug_session_repo,
            debug_session_span_repository=self.debug_session_span_repo,
            run_repository=self.run_repository,
            cycle_run_repository=self.cycle_run_repository,
            tick_session_repository=self.tick_session_repo,
            trade_fill_repository=self.trade_fill_repository,
            model_route_repository=self.model_route_repo,
        )

    async def _wait_for_debug_status(self, service: TradingPlatformService, task_id: str, session_id: str):
        for _ in range(40):
            session = await service.get_debug_session(task_id, session_id)
            if session["status"] in {"completed", "failed"}:
                return session
            await asyncio.sleep(0.01)
        self.fail("debug session did not finish in time")

    async def test_create_start_and_stop_task(self):
        service = self._build_service()

        instance = await service.create_task(
            name="alpha", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)
        await service.tick_once()

        status = await service.get_task_status(instance.task_id)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["cycles"], 1)

        await service.stop_task(instance.task_id)
        status_after_stop = await service.get_task_status(instance.task_id)
        self.assertEqual(status_after_stop["status"], "stopped")

    async def test_start_task_rejects_backtest_mode_task(self):
        service = self._build_service()
        task = await service.create_task(
            name="bt-start-reject",
            template_id="single-agent-trend",
            mode="backtest",
            settings=self._inst(),
        )

        with self.assertRaisesRegex(ValueError, "backtest task does not support start"):
            await service.start_task(task.task_id)

    async def test_start_backtest_job_rejects_non_backtest_mode_task(self):
        service = self._build_service()
        task = await service.create_task(
            name="paper-run-reject",
            template_id="single-agent-trend",
            mode="paper",
            settings=self._inst(),
        )

        with self.assertRaisesRegex(ValueError, "runs are only supported for backtest tasks"):
            await service.start_backtest_job(
                task.task_id,
                range_start="2024-01-02",
                range_end="2024-01-31",
            )

    async def test_start_backtest_job_rejects_backtest_task_with_existing_run(self):
        service = self._build_service()
        task = await service.create_task(
            name="bt-single-run",
            template_id="single-agent-trend",
            mode="backtest",
            settings=self._inst(),
        )
        await self.run_repository.create_pending(
            run_id=f"seed-{uuid.uuid4()}",
            task_id=task.task_id,
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2024, 1, 2),
            range_end_utc=datetime(2024, 1, 31),
            session_id=f"seed-session-{uuid.uuid4()}",
            bars_total=20,
            model_route_name=self._default_model_route,
        )

        with self.assertRaisesRegex(ValueError, "backtest task already has a run"):
            await service.start_backtest_job(
                task.task_id,
                range_start="2024-01-02",
                range_end="2024-01-31",
            )

    async def test_stop_backtest_job_marks_run_and_debug_session_stopped(self):
        service = self._build_service()
        task = await service.create_task(
            name="bt-stop-status",
            template_id="single-agent-trend",
            mode="backtest",
            settings=self._inst(),
        )

        session_id = f"backtest-{uuid.uuid4()}"
        run_id = f"btjob-{uuid.uuid4()}"
        await self.debug_session_repo.create_session(
            session_id=session_id,
            task_id=task.task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="backtest",
        )
        await self.run_repository.create_pending(
            run_id=run_id,
            task_id=task.task_id,
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2024, 1, 2),
            range_end_utc=datetime(2024, 1, 31),
            session_id=session_id,
            bars_total=20,
            model_route_name=self._default_model_route,
        )
        await self.run_repository.mark_running(run_id)
        await self.debug_session_repo.mark_running(
            session_id,
            run_id=run_id,
            effective_config=None,
        )

        stopped = await service.stop_backtest_job(task.task_id, run_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertIn(stopped.get("error_message"), ("", None))

        session = await service.get_debug_session(task.task_id, session_id)
        self.assertEqual(session["status"], "stopped")
        self.assertIn(session.get("error_message"), ("", None))

        with self.assertRaisesRegex(RuntimeError, "already finished"):
            await service.stop_backtest_job(task.task_id, run_id)

    async def test_recover_running_tasks_from_repository(self):
        service = self._build_service()
        instance = await service.create_task(
            name="alpha", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)

        restored_service = self._build_service()
        recovered = await restored_service.restore_tasks()

        self.assertEqual(recovered, 1)
        status = await restored_service.get_task_status(instance.task_id)
        self.assertEqual(status["status"], "running")

    async def test_kill_switch_blocks_restore_of_running_tasks(self):
        service = self._build_service()
        instance = await service.create_task(
            name="beta", template_id="single-agent-trend", settings=self._inst()
        )
        await self.system_state_repository.set_kill_switch_enabled(True)
        await self.task_repository.update_status(instance.task_id, "running", "")

        restored_service = self._build_service()
        recovered = await restored_service.restore_tasks()

        self.assertEqual(recovered, 0)
        status = await restored_service.get_task_status(instance.task_id)
        self.assertEqual(status["status"], "running")
        system_state = await restored_service.get_system_state()
        self.assertTrue(system_state["kill_switch_enabled"])

    async def test_tick_once_honors_persisted_kill_switch_changes(self):
        service = self._build_service()
        instance = await service.create_task(
            name="epsilon", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)
        await self.system_state_repository.set_kill_switch_enabled(True)

        executed = await service.tick_once()
        status = await service.get_task_status(instance.task_id)

        self.assertEqual(executed, 0)
        self.assertEqual(status["cycles"], 0)
        self.assertEqual(status["status"], "running")

    async def test_set_kill_switch_stops_running_tasks_in_repository_state(self):
        service = self._build_service()
        instance = await service.create_task(
            name="zeta", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)

        await service.set_kill_switch(True)

        status = await service.get_task_status(instance.task_id)
        system_state = await service.get_system_state()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(system_state["running_count"], 0)
        self.assertTrue(system_state["kill_switch_enabled"])

    async def test_restore_failure_marks_task_error(self):
        service = self._build_service()
        instance = await service.create_task(
            name="gamma", template_id="single-agent-trend", settings=self._inst()
        )
        await self.task_repository.update_status(instance.task_id, "running", "")

        restored_service = self._build_service(
            worker_factory=lambda config, ms, acct=None: (_ for _ in ()).throw(RuntimeError("restore failed"))
        )
        recovered = await restored_service.restore_tasks()

        self.assertEqual(recovered, 0)
        status = await restored_service.get_task_status(instance.task_id)
        self.assertEqual(status["status"], "error")
        self.assertIn("restore failed", status["last_error"])

    async def test_get_local_market_bars_returns_summary_and_coverage(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 10.0,
                    "high": 10.8,
                    "low": 9.9,
                    "close": 10.6,
                    "volume": 1000.0,
                    "amount": 10500.0,
                },
                {
                    "timestamp": "2026-01-03T00:00:00+00:00",
                    "open": 10.6,
                    "high": 11.0,
                    "low": 10.5,
                    "close": 10.9,
                    "volume": 1200.0,
                    "amount": 13080.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "1d",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-01T00:00:00+00:00",
                "covered_end": "2026-01-31T00:00:00+00:00",
                "status": "ok",
            },
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-31",
        )

        self.assertEqual(payload["summary"]["bar_count"], 2)
        self.assertEqual(payload["summary"]["latest_close"], 10.9)
        self.assertAlmostEqual(payload["summary"]["window_change"], 0.9)
        self.assertEqual(payload["summary"]["total_volume"], 2200.0)
        self.assertEqual(payload["coverage"]["requested_start"], "2026-01-01")
        self.assertEqual(payload["coverage"]["requested_end"], "2026-01-31")
        self.assertEqual(
            payload["coverage"]["covered_segments"],
            [{"start": "2026-01-01", "end": "2026-01-31", "status": "covered"}],
        )
        self.assertEqual(payload["coverage"]["missing_segments"], [])

    async def test_get_local_market_bars_5m_reports_partial_intraday_coverage(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T09:30:00+00:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                },
                {
                    "timestamp": "2026-01-02T09:35:00+00:00",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 10.0,
                    "close": 10.1,
                    "volume": 120.0,
                    "amount": 1212.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "5m",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-02T09:30:00+00:00",
                "covered_end": "2026-01-02T09:35:00+00:00",
                "status": "ok",
            },
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="5m",
            start="2026-01-02T09:30:00+00:00",
            end="2026-01-02T10:00:00+00:00",
        )

        self.assertEqual(
            payload["coverage"]["covered_segments"],
            [
                {
                    "start": "2026-01-02T09:30:00+00:00",
                    "end": "2026-01-02T09:35:00+00:00",
                    "status": "covered",
                }
            ],
        )
        self.assertEqual(
            payload["coverage"]["missing_segments"],
            [
                {
                    "start": "2026-01-02T09:40:00+00:00",
                    "end": "2026-01-02T10:00:00+00:00",
                    "status": "missing",
                }
            ],
        )

    async def test_get_local_market_bars_5m_accepts_canonical_naive_row_timestamps(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T09:30:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                },
                {
                    "timestamp": "2026-01-02T09:35:00",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 10.0,
                    "close": 10.1,
                    "volume": 120.0,
                    "amount": 1212.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "5m",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-02T09:30:00+00:00",
                "covered_end": "2026-01-02T09:35:00+00:00",
                "status": "ok",
            },
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="5m",
            start="2026-01-02T09:30:00+00:00",
            end="2026-01-02T10:00:00+00:00",
        )

        self.assertEqual(payload["bars"][0]["timestamp"], "2026-01-02T09:30:00")
        self.assertEqual(
            payload["coverage"]["covered_segments"],
            [
                {
                    "start": "2026-01-02T09:30:00+00:00",
                    "end": "2026-01-02T09:35:00+00:00",
                    "status": "covered",
                }
            ],
        )
        self.assertEqual(
            payload["coverage"]["missing_segments"],
            [
                {
                    "start": "2026-01-02T09:40:00+00:00",
                    "end": "2026-01-02T10:00:00+00:00",
                    "status": "missing",
                }
            ],
        )

    async def test_get_local_market_bars_rejects_reversed_intraday_bounds(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(bars=[], sync_state=None)

        with self.assertRaisesRegex(ValueError, "requested_end must be on or after requested_start"):
            await service.get_local_market_bars(
                symbol="600000.SH",
                interval="5m",
                start="2026-01-02T10:00:00+00:00",
                end="2026-01-02T09:30:00+00:00",
            )

    async def test_get_local_market_bars_5m_date_only_bounds_do_not_invent_off_hours_missing(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T09:30:00+00:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                },
                {
                    "timestamp": "2026-01-02T15:00:00+00:00",
                    "open": 10.2,
                    "high": 10.3,
                    "low": 10.1,
                    "close": 10.2,
                    "volume": 120.0,
                    "amount": 1224.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "5m",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-02T09:30:00+00:00",
                "covered_end": "2026-01-02T15:00:00+00:00",
                "status": "ok",
            },
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="5m",
            start="2026-01-02",
            end="2026-01-02",
        )

        self.assertEqual(
            payload["coverage"]["covered_segments"],
            [
                {
                    "start": "2026-01-02T09:30:00+00:00",
                    "end": "2026-01-02T15:00:00+00:00",
                    "status": "covered",
                }
            ],
        )
        self.assertEqual(payload["coverage"]["missing_segments"], [])

    async def test_get_local_market_bars_1d_without_sync_state_does_not_invent_weekend_gaps(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000.0,
                    "amount": 10100.0,
                },
                {
                    "timestamp": "2026-01-05T00:00:00+00:00",
                    "open": 10.2,
                    "high": 10.4,
                    "low": 10.1,
                    "close": 10.3,
                    "volume": 1200.0,
                    "amount": 12360.0,
                },
            ],
            sync_state=None,
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-02",
            end="2026-01-05",
        )

        self.assertEqual(
            payload["coverage"]["covered_segments"],
            [{"start": "2026-01-02", "end": "2026-01-05", "status": "covered"}],
        )
        self.assertEqual(payload["coverage"]["missing_segments"], [])

    async def test_sync_local_market_bars_range_small_window_runs_inline(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(bars=[], sync_state=None)
        runner = _FakeRangeSyncRunner(result_count=42)
        service._market_data_sync_runner = runner

        payload = await service.sync_local_market_bars_range(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-03-01",
            provider="auto",
            adjust="qfq",
            mode="fill_gap",
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["execution_mode"], "sync")
        self.assertEqual(payload["upserted_count"], 42)
        self.assertEqual(
            runner.calls,
            [
                {
                    "symbol": "600000.SH",
                    "interval": "1d",
                    "start": "2026-01-01",
                    "end": "2026-03-01",
                    "provider": "auto",
                    "adjust": "qfq",
                    "mode": "fill_gap",
                }
            ],
        )

    async def test_sync_local_market_bars_range_large_window_returns_async_job(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(bars=[], sync_state=None)
        wait_event = asyncio.Event()
        service._market_data_sync_runner = _FakeRangeSyncRunner(result_count=99, wait_event=wait_event)

        payload = await service.sync_local_market_bars_range(
            symbol="600000.SH",
            interval="5m",
            start="2025-01-01",
            end="2026-01-01",
            provider="auto",
            adjust="qfq",
            mode="force_refresh",
        )

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["execution_mode"], "async")
        self.assertIn("job_id", payload)
        await asyncio.sleep(0)
        job = await service.get_local_market_sync_job(payload["job_id"])
        self.assertIn(job["status"], {"pending", "running"})
        self.assertEqual(
            job["requested_range"],
            {"start": "2025-01-01", "end": "2026-01-01"},
        )
        self.assertIn("started_at", job)
        self.assertIn("finished_at", job)
        self.assertIn("error_code", job)
        self.assertIn("error_type", job)
        self.assertIn("error_message", job)
        self.assertIn("hint", job)
        wait_event.set()

    async def test_get_local_market_sync_job_reports_ok_after_async_run(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(bars=[], sync_state=None)
        wait_event = asyncio.Event()
        service._market_data_sync_runner = _FakeRangeSyncRunner(result_count=99, wait_event=wait_event)

        accepted = await service.sync_local_market_bars_range(
            symbol="600000.SH",
            interval="5m",
            start="2025-01-01",
            end="2026-01-01",
            provider="auto",
            adjust="qfq",
            mode="force_refresh",
        )
        await asyncio.sleep(0)
        wait_event.set()

        for _ in range(40):
            job = await service.get_local_market_sync_job(accepted["job_id"])
            if job["status"] == "ok":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("local market sync job did not complete in time")

        self.assertEqual(job["upserted_count"], 99)
        self.assertEqual(
            job["fetched_segments"],
            [{"start": "2025-01-01", "end": "2026-01-01", "status": "fetched"}],
        )

    async def test_sync_local_market_bars_range_direct_fill_gap_skips_when_local_bars_cover_weekdays(self):
        service = self._build_service()
        repo = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10200.0,
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 10.1,
                    "high": 10.6,
                    "low": 10.0,
                    "close": 10.3,
                    "volume": 1100.0,
                    "amount": 11330.0,
                },
                {
                    "timestamp": "2026-01-05T00:00:00+00:00",
                    "open": 10.2,
                    "high": 10.7,
                    "low": 10.1,
                    "close": 10.4,
                    "volume": 1200.0,
                    "amount": 12480.0,
                },
            ],
            sync_state=None,
        )
        provider = _FakeBarsDataProvider(
            bars=[
                _FakeBar(
                    symbol="600000.SH",
                    timestamp="2026-01-02",
                    open=10.0,
                    high=10.5,
                    low=9.9,
                    close=10.2,
                    volume=1000.0,
                    amount=10200.0,
                    adjust_type="qfq",
                )
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-01",
                end="2026-01-05",
                provider="auto",
                adjust="qfq",
                mode="fill_gap",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["fetched_segments"], [])
        self.assertEqual(payload["upserted_count"], 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(repo.upsert_bars_calls, [])

    async def test_sync_local_market_bars_range_direct_fill_gap_repairs_internal_missing_weekday(self):
        service = self._build_service()
        repo = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10200.0,
                },
                {
                    "timestamp": "2026-01-05T00:00:00+00:00",
                    "open": 10.2,
                    "high": 10.7,
                    "low": 10.1,
                    "close": 10.4,
                    "volume": 1200.0,
                    "amount": 12480.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "1d",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-01T00:00:00+00:00",
                "covered_end": "2026-01-05T23:59:59.999999+00:00",
                "status": "ok",
            },
        )
        provider = _FakeBarsDataProvider(
            bars=[
                _FakeBar(
                    symbol="600000.SH",
                    timestamp="2026-01-02",
                    open=10.1,
                    high=10.6,
                    low=10.0,
                    close=10.3,
                    volume=1100.0,
                    amount=11330.0,
                    adjust_type="qfq",
                )
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-01",
                end="2026-01-05",
                provider="auto",
                adjust="qfq",
                mode="fill_gap",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["fetched_segments"],
            [{"start": "2026-01-02", "end": "2026-01-02", "status": "fetched"}],
        )
        self.assertEqual(payload["upserted_count"], 1)
        self.assertFalse(payload["adjust_drift_refreshed"])
        # fill_gap widens the fetch backwards to anchor days (clamped at the
        # earliest local bar) so adjust-factor drift can be detected.
        self.assertEqual(
            provider.calls,
            [
                {
                    "symbol": "600000.SH",
                    "start": "2026-01-01",
                    "end": "2026-01-02",
                    "interval": "1d",
                }
            ],
        )

    async def test_sync_local_market_bars_range_direct_force_refresh_fetches_despite_coverage(self):
        service = self._build_service()
        repo = _FakeMarketBarsRepository(
            bars=[],
            sync_state={
                "symbol": "600000.SH",
                "interval": "1d",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2026-01-01T00:00:00+00:00",
                "covered_end": "2026-03-01T23:59:59.999999+00:00",
                "status": "ok",
            },
        )
        provider = _FakeBarsDataProvider(
            bars=[
                _FakeBar(
                    symbol="600000.SH",
                    timestamp="2026-01-02",
                    open=10.0,
                    high=10.5,
                    low=9.9,
                    close=10.2,
                    volume=1000.0,
                    amount=10200.0,
                    adjust_type="qfq",
                )
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-01",
                end="2026-03-01",
                provider="auto",
                adjust="qfq",
                mode="force_refresh",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["fetched_segments"],
            [{"start": "2026-01-01", "end": "2026-03-01", "status": "fetched"}],
        )
        self.assertEqual(payload["upserted_count"], 1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(repo.upsert_bars_calls), 1)
        self.assertEqual(len(repo.mark_sync_success_calls), 1)
        self.assertEqual(
            repo.mark_sync_success_calls[0]["target_start"],
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )

    async def test_sync_local_market_bars_range_fill_gap_adjust_drift_triggers_full_refresh(self):
        service = self._build_service()
        # Locally cached bars still carry the pre-ex-rights adjust factor (~10x).
        repo = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "open": 102.0,
                    "high": 102.0,
                    "low": 102.0,
                    "close": 102.0,
                    "volume": 1000.0,
                    "amount": 102000.0,
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 103.0,
                    "high": 103.0,
                    "low": 103.0,
                    "close": 103.0,
                    "volume": 1100.0,
                    "amount": 113300.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "1d",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2025-12-01T00:00:00+00:00",
                "covered_end": "2026-01-02T23:59:59.999999+00:00",
                "status": "ok",
            },
        )
        provider = _ScriptedBarsDataProvider(
            responses=[
                # Anchored fill_gap fetch: fresh factor disagrees on anchor days.
                [
                    _daily_fake_bar("2026-01-01", 10.2),
                    _daily_fake_bar("2026-01-02", 10.3),
                    _daily_fake_bar("2026-01-05", 10.4),
                ],
                # Escalated full refresh over the whole local coverage.
                [
                    _daily_fake_bar("2025-12-01", 10.0),
                    _daily_fake_bar("2026-01-01", 10.2),
                    _daily_fake_bar("2026-01-02", 10.3),
                    _daily_fake_bar("2026-01-05", 10.4),
                ],
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-01",
                end="2026-01-05",
                provider="auto",
                adjust="qfq",
                mode="fill_gap",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["adjust_drift_refreshed"])
        self.assertIn(
            "检测到复权因子变化（除权/除息），已自动全量重刷本地K线缓存",
            payload["warnings"],
        )
        self.assertEqual(
            payload["fetched_segments"],
            [{"start": "2025-12-01", "end": "2026-01-05", "status": "fetched"}],
        )
        self.assertEqual(payload["upserted_count"], 4)
        # First call is the anchored gap fetch, second the wholesale refresh.
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["start"], "2026-01-01")
        self.assertEqual(provider.calls[0]["end"], "2026-01-05")
        self.assertEqual(provider.calls[1]["start"], "2025-12-01")
        self.assertEqual(provider.calls[1]["end"], "2026-01-05")
        self.assertEqual(len(repo.upsert_bars_calls), 1)
        self.assertEqual(len(repo.upsert_bars_calls[0]["bars"]), 4)
        self.assertEqual(len(repo.mark_sync_success_calls), 1)
        self.assertEqual(
            repo.mark_sync_success_calls[0]["target_start"],
            datetime(2025, 12, 1, 0, 0, tzinfo=timezone.utc),
        )

    async def test_sync_local_market_bars_range_force_refresh_adjust_drift_extends_beyond_window(self):
        service = self._build_service()
        # The requested window itself overlaps cached bars; older lazily-loaded
        # history (per sync_state covered_start) sits outside the window and
        # must be refreshed too once drift is detected.
        repo = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 103.0,
                    "high": 103.0,
                    "low": 103.0,
                    "close": 103.0,
                    "volume": 1100.0,
                    "amount": 113300.0,
                },
                {
                    "timestamp": "2026-01-05T00:00:00+00:00",
                    "open": 104.0,
                    "high": 104.0,
                    "low": 104.0,
                    "close": 104.0,
                    "volume": 1200.0,
                    "amount": 124800.0,
                },
            ],
            sync_state={
                "symbol": "600000.SH",
                "interval": "1d",
                "provider": "auto",
                "adjust": "qfq",
                "covered_start": "2025-12-15T00:00:00+00:00",
                "covered_end": "2026-01-05T23:59:59.999999+00:00",
                "status": "ok",
            },
        )
        provider = _ScriptedBarsDataProvider(
            responses=[
                # force_refresh window fetch: fresh factor disagrees in-window.
                [
                    _daily_fake_bar("2026-01-02", 10.3),
                    _daily_fake_bar("2026-01-05", 10.4),
                ],
                # Escalated full refresh from the older local coverage start.
                [
                    _daily_fake_bar("2025-12-15", 10.0),
                    _daily_fake_bar("2026-01-02", 10.3),
                    _daily_fake_bar("2026-01-05", 10.4),
                ],
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-02",
                end="2026-01-05",
                provider="auto",
                adjust="qfq",
                mode="force_refresh",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["adjust_drift_refreshed"])
        self.assertIn(
            "检测到复权因子变化（除权/除息），已自动全量重刷本地K线缓存",
            payload["warnings"],
        )
        self.assertEqual(
            payload["fetched_segments"],
            [{"start": "2025-12-15", "end": "2026-01-05", "status": "fetched"}],
        )
        self.assertEqual(payload["upserted_count"], 3)
        # force_refresh does not anchor-expand the first fetch.
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["start"], "2026-01-02")
        self.assertEqual(provider.calls[1]["start"], "2025-12-15")
        self.assertEqual(provider.calls[1]["end"], "2026-01-05")
        self.assertEqual(len(repo.upsert_bars_calls), 1)
        self.assertEqual(len(repo.upsert_bars_calls[0]["bars"]), 3)

    async def test_sync_local_market_bars_range_fill_gap_anchor_overlap_without_drift_keeps_behavior(self):
        service = self._build_service()
        repo = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "open": 10.2,
                    "high": 10.2,
                    "low": 10.2,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10200.0,
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 10.3,
                    "high": 10.3,
                    "low": 10.3,
                    "close": 10.3,
                    "volume": 1100.0,
                    "amount": 11330.0,
                },
            ],
            sync_state=None,
        )
        provider = _ScriptedBarsDataProvider(
            responses=[
                # Anchored fetch returns anchor days matching the cached closes
                # exactly: no drift, normal fill_gap upsert.
                [
                    _daily_fake_bar("2026-01-01", 10.2),
                    _daily_fake_bar("2026-01-02", 10.3),
                    _daily_fake_bar("2026-01-05", 10.4),
                ],
            ]
        )
        service.market_bars_repository = repo
        service._market_data_sync_runner = None

        with patch(
            "doyoutrade.platform.service.build_trading_data_stack",
            return_value=(provider, None, None),
        ):
            payload = await service.sync_local_market_bars_range(
                symbol="600000.SH",
                interval="1d",
                start="2026-01-01",
                end="2026-01-05",
                provider="auto",
                adjust="qfq",
                mode="fill_gap",
            )

        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["adjust_drift_refreshed"])
        self.assertNotIn(
            "检测到复权因子变化（除权/除息），已自动全量重刷本地K线缓存",
            payload["warnings"],
        )
        # Reported segments keep the originally missing window even though the
        # actual fetch was anchor-expanded (idempotent upsert of anchor days).
        self.assertEqual(
            payload["fetched_segments"],
            [{"start": "2026-01-05", "end": "2026-01-05", "status": "fetched"}],
        )
        self.assertEqual(payload["upserted_count"], 3)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0]["start"], "2026-01-01")
        self.assertEqual(provider.calls[0]["end"], "2026-01-05")
        self.assertEqual(len(repo.upsert_bars_calls), 1)

    async def test_get_local_market_bars_includes_available_overlay_candidates(self):
        service = self._build_service()
        service.market_bars_repository = _FakeMarketBarsRepository(
            bars=[
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "open": 10.0,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10200.0,
                }
            ],
            sync_state=None,
        )
        backtest_task = await service.create_task(
            name="overlay-backtest",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst({"universe": ["600000.SH"]}),
        )
        live_task = await service.create_task(
            name="overlay-live",
            template_id="single-agent-trend",
            mode="paper",
            data_provider="mock",
            settings=self._inst({"universe": ["600000.SH"]}),
        )
        await self.run_repository.create_pending(
            run_id="bt-overlay-run",
            task_id=backtest_task.task_id,
            mode="backtest",
            market_profile="cn_equity",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1),
            range_end_utc=datetime(2026, 1, 31),
            session_id="bt-session-1",
            bars_total=10,
        )
        await self.run_repository.finalize_success(
            "bt-overlay-run",
            starting_equity=100000.0,
            ending_equity=101000.0,
            return_pct=0.01,
        )
        await self.trade_fill_repository.insert_fill(
            task_id=backtest_task.task_id,
            cycle_run_id="bt-cycle-1",
            run_id="bt-overlay-run",
            session_id="bt-session-1",
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.2",
            amount="1020",
            fee=None,
            currency=None,
            intent_id="bt-intent-1",
            rationale="backtest.entry",
            filled_at=datetime(2026, 1, 3, 0, 0, 0),
            source_mode="backtest",
            raw_payload=None,
        )
        await self.run_repository.create_pending(
            run_id="paper-overlay-run",
            task_id=live_task.task_id,
            mode="paper",
            market_profile="cn_equity",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1),
            range_end_utc=datetime(2026, 1, 31),
            session_id="paper-session-1",
            bars_total=5,
        )
        await self.run_repository.mark_running("paper-overlay-run")
        await self.trade_fill_repository.insert_fill(
            task_id=live_task.task_id,
            cycle_run_id="paper-cycle-1",
            run_id="paper-overlay-run",
            session_id="paper-session-1",
            symbol="600000.SH",
            side="sell",
            quantity="50",
            price="10.6",
            amount="530",
            fee=None,
            currency=None,
            intent_id="paper-intent-1",
            rationale="live.exit",
            filled_at=datetime(2026, 1, 4, 0, 0, 0),
            source_mode="paper",
            raw_payload=None,
        )
        await self.cycle_run_repository.create_started(
            run_id="paper-cycle-1",
            task_id=live_task.task_id,
            agent_name="ag",
            session_id="paper-session-1",
            trace_id=None,
            run_mode="paper",
            run_kind="scheduled",
            clock_mode="wall",
            cycle_time=datetime(2026, 1, 4, 0, 0, 0),
            runtime_params=None,
        )
        await self.cycle_run_repository.finalize(
            "paper-cycle-1",
            status="completed",
            details_patch={
                "decisions": [
                    {
                        "symbol": "600000.SH",
                        "side": "buy",
                        "price": 10.4,
                        "label": "BUY",
                    }
                ]
            },
        )

        payload = await service.get_local_market_bars(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-31",
        )

        self.assertIn("available_overlays", payload)
        self.assertTrue(payload["available_overlays"]["backtest_trades"])
        self.assertTrue(payload["available_overlays"]["task_fills"])
        self.assertTrue(payload["available_overlays"]["signals"])

    async def test_get_local_market_overlays_normalizes_backtest_trades(self):
        service = self._build_service()
        task = await service.create_task(
            name="overlay-backtest",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst({"universe": ["600000.SH"]}),
        )
        await self.run_repository.create_pending(
            run_id="bt-overlay-run",
            task_id=task.task_id,
            mode="backtest",
            market_profile="cn_equity",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1),
            range_end_utc=datetime(2026, 1, 31),
            session_id="bt-session-1",
            bars_total=10,
        )
        await self.run_repository.finalize_success(
            "bt-overlay-run",
            starting_equity=100000.0,
            ending_equity=101000.0,
            return_pct=0.01,
        )
        await self.trade_fill_repository.insert_fill(
            task_id=task.task_id,
            cycle_run_id="bt-cycle-1",
            run_id="bt-overlay-run",
            session_id="bt-session-1",
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.2",
            amount="1020",
            fee=None,
            currency=None,
            intent_id="bt-intent-1",
            rationale="factor.macd.golden_cross",
            filled_at=datetime(2026, 1, 3, 0, 0, 0),
            source_mode="backtest",
            raw_payload=None,
        )

        payload = await service.get_local_market_overlays(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-31",
            overlay_kind="backtest_trades",
            run_id="bt-overlay-run",
        )

        self.assertEqual(payload["overlay_kind"], "backtest_trades")
        self.assertEqual(payload["source"]["run_id"], "bt-overlay-run")
        self.assertEqual(payload["items"][0]["side"], "buy")
        self.assertEqual(payload["items"][0]["price"], 10.2)
        self.assertEqual(payload["items"][0]["label"], "BUY")
        self.assertEqual(payload["items"][0]["details"]["intent_id"], "bt-intent-1")

    async def test_get_local_market_overlays_normalizes_signal_items(self):
        service = self._build_service()
        task = await service.create_task(
            name="overlay-live",
            template_id="single-agent-trend",
            mode="paper",
            data_provider="mock",
            settings=self._inst({"universe": ["600000.SH"]}),
        )
        await self.cycle_run_repository.create_started(
            run_id="paper-cycle-1",
            task_id=task.task_id,
            agent_name="ag",
            session_id="paper-session-1",
            trace_id=None,
            run_mode="paper",
            run_kind="scheduled",
            clock_mode="wall",
            cycle_time=datetime(2026, 1, 4, 0, 0, 0),
            runtime_params=None,
        )
        await self.cycle_run_repository.finalize(
            "paper-cycle-1",
            status="completed",
            details_patch={
                "decisions": [
                    {
                        "symbol": "600000.SH",
                        "side": "buy",
                        "price": 10.4,
                        "label": "breakout",
                    }
                ]
            },
        )

        payload = await service.get_local_market_overlays(
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-31",
            overlay_kind="signals",
            signal_source_id=task.task_id,
        )

        self.assertEqual(payload["overlay_kind"], "signals")
        self.assertEqual(payload["source"]["task_id"], task.task_id)
        self.assertEqual(payload["items"][0]["kind"], "signal")
        self.assertEqual(payload["items"][0]["side"], "buy")
        self.assertEqual(payload["items"][0]["price"], 10.4)
        self.assertEqual(payload["items"][0]["label"], "BREAKOUT")

    async def test_scheduler_error_persists_to_repository(self):
        service = self._build_service(worker_factory=lambda config, ms, acct=None: _FailingWorker("tick failed"))
        instance = await service.create_task(
            name="delta", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)

        executed = await service.tick_once()

        self.assertEqual(executed, 0)
        status = await service.get_task_status(instance.task_id)
        self.assertEqual(status["status"], "error")
        self.assertIn("tick failed", status["last_error"])

    async def test_get_task_status_returns_all_task_business_fields(self):
        service = self._build_service()
        # account_id is now a FK into the accounts table — create the bound
        # account so create_task's validation passes.
        await self.account_repo.upsert_account(
            {"id": "acct-9", "name": "acct-9", "mode": "mock", "base_url": ""}
        )

        instance = await service.create_task(
            name="theta",
            template_id="single-agent-event",
            mode="live",
            orchestrator_mode="multi-role",
            description="swing trader",
            data_provider="mock",
            settings=self._inst(
                {
                    "risk": "medium",
                    "watch_symbols": ["AAPL", "MSFT"],
                    "universe": ["MSFT", "GOOG"],
                    "execution_strategy": "langchain",
                    "account_id": "acct-9",
                    "model_id": "gpt-4.1",
                }
            ),
        )

        status = await service.get_task_status(instance.task_id)

        self.assertEqual(status["task_id"], instance.task_id)
        self.assertEqual(status["name"], "theta")
        self.assertEqual(status["mode"], "live")
        self.assertEqual(status["description"], "swing trader")
        self.assertEqual(status["data_provider"], "mock")
        self.assertEqual(status["data_provider_effective"], "mock")
        self.assertEqual(status["universe"], ["MSFT", "GOOG"])
        self.assertEqual(status["execution_strategy"], "langchain")
        self.assertEqual(status["account_id"], "acct-9")
        self.assertEqual(status["model_id"], "gpt-4.1")
        # Agent task without a bound strategy definition -> strategy_name is None.
        self.assertIsNone(status["strategy_name"])
        self.assertNotIn("template_id", status)
        self.assertNotIn("orchestrator_mode", status)
        self.assertNotIn("watch_symbols", status)
        self.assertEqual(status["settings"]["name"], "theta")
        self.assertEqual(status["settings"]["mode"], "live")
        self.assertEqual(status["settings"]["description"], "swing trader")
        self.assertEqual(status["settings"]["data_provider"], "mock")
        self.assertEqual(status["settings"]["universe"], ["MSFT", "GOOG"])
        self.assertEqual(status["settings"]["strategy_preferences"], "langchain")
        self.assertEqual(status["settings"]["model_route_name"], self._default_model_route)
        self.assertNotIn("signal_mode", status["settings"])
        self.assertNotIn("template_id", status["settings"])
        self.assertNotIn("orchestrator_mode", status["settings"])
        self.assertNotIn("watch_symbols", status["settings"])
        self.assertEqual(status["settings"]["agent"]["react_max_turns"], DEFAULT_REACT_MAX_TURNS)
        self.assertEqual(status["settings"]["agent"]["signal_tool_names"], list(DEFAULT_SIGNAL_TOOL_NAMES))
        self.assertEqual(status["settings"]["agent"]["enabled_skills"], [])
        # position_constraints and approval use defaults from config
        self.assertIn("position_constraints", status["settings"]["agent"])
        self.assertIn("approval", status["settings"]["agent"])
        self.assertTrue(status["created_at"])
        self.assertTrue(status["updated_at"])

    async def test_get_task_status_resolves_strategy_name_from_definition(self):
        from types import SimpleNamespace

        class _FakeDefinitionRepo:
            async def get_definition(self, definition_id):
                assert definition_id == "sd-keep-me"
                return SimpleNamespace(definition_id=definition_id, name="超跌反抽")

        service = self._build_service()
        instance = await service.create_task(
            name="named-strategy",
            mode="backtest",
            settings=merge_task_settings(
                {
                    "universe": ["300058.SZ"],
                    "strategy": {"definition_id": "sd-keep-me"},
                }
            ),
        )

        # No strategy runtime wired -> strategy_name stays None (best-effort).
        before = await service.get_task_status(instance.task_id)
        self.assertIsNone(before["strategy_name"])

        # Wire a definition repository -> strategy_name resolves to the name.
        service.strategy_runtime = SimpleNamespace(definition_repository=_FakeDefinitionRepo())
        after = await service.get_task_status(instance.task_id)
        self.assertEqual(after["strategy_name"], "超跌反抽")

    async def test_update_task_with_only_data_provider_preserves_settings(self):
        """Regression for tmp/error_request.json: an update_task that only
        patched ``data_provider`` used to wipe the persisted ``settings`` (the
        service forwarded ``settings=None`` to the repo, which treated that as
        a wipe). The strategy binding, universe and agent block must survive.
        """

        service = self._build_service()
        instance = await service.create_task(
            name="update-preserve",
            mode="backtest",
            settings=merge_task_settings(
                {
                    "universe": ["300058.SZ"],
                    "strategy": {"definition_id": "sd-keep-me"},
                    "agent": {
                        "react_max_turns": 4,
                        "signal_tool_names": ["data_bars_relative"],
                    },
                }
            ),
        )

        before = await service.get_task_status(instance.task_id)
        self.assertEqual(
            before["settings"]["strategy"]["definition_id"], "sd-keep-me"
        )
        self.assertEqual(before["settings"]["universe"], ["300058.SZ"])
        self.assertEqual(before["settings"]["agent"]["react_max_turns"], 4)

        await service.update_task(instance.task_id, data_provider="akshare")

        after = await service.get_task_status(instance.task_id)
        self.assertEqual(after["data_provider"], "akshare")
        self.assertEqual(
            after["settings"]["strategy"]["definition_id"],
            "sd-keep-me",
            msg="strategy binding must survive a top-level-only update",
        )
        self.assertEqual(after["settings"]["universe"], ["300058.SZ"])
        self.assertEqual(after["settings"]["agent"]["react_max_turns"], 4)

    async def test_update_task_rejects_switch_between_trading_and_backtest(self):
        service = self._build_service()
        trading = await service.create_task(
            name="trade-task",
            mode="paper",
            settings=merge_task_settings(
                {
                    "universe": ["300058.SZ"],
                    "strategy": {"definition_id": "sd-keep-me"},
                }
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot switch task mode between trading and backtest",
        ):
            await service.update_task(trading.task_id, mode="backtest")

        backtest = await service.create_task(
            name="backtest-task",
            mode="backtest",
            settings=merge_task_settings(
                {
                    "universe": ["300058.SZ"],
                    "strategy": {"definition_id": "sd-keep-me"},
                }
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot switch task mode between trading and backtest",
        ):
            await service.update_task(backtest.task_id, mode="live")

    async def test_create_task_allows_missing_model_route_name_for_strategy_instance(self):
        service = self._build_service()

        instance = await service.create_task(
            name="theta-no-route",
            mode="paper",
            settings=merge_task_settings(
                {
                    "universe": ["MSFT"],
                    "strategy": {"definition_id": "sd-main"},
                }
            ),
        )

        status = await service.get_task_status(instance.task_id)

        self.assertEqual(status["task_id"], instance.task_id)
        self.assertNotIn("model_route_name", status["settings"])

    async def test_get_task_status_omits_deprecated_fields_for_legacy_record_values(self):
        service = self._build_service()
        record = await self.task_repository.create_task(
            task_id=f"legacy-{uuid.uuid4()}",
            name="legacy-read",
            template_id="legacy-template-id",
            mode="paper",
            orchestrator_mode="legacy-orchestrator",
            description="legacy payload",
            data_provider="mock",
            status="configured",
            last_error="",
            settings={
                "model_route_name": self._default_model_route,
                "watch_symbols": ["600000.SH", "000001.SZ"],
                "universe": ["600000.SH"],
                "execution_strategy": "langchain",
                "account_id": "acct-legacy",
                "model_id": "model-legacy",
            },
        )

        status = await service.get_task_status(record.task_id)

        self.assertNotIn("template_id", status)
        self.assertNotIn("orchestrator_mode", status)
        self.assertNotIn("watch_symbols", status)
        self.assertNotIn("template_id", status["settings"])
        self.assertNotIn("orchestrator_mode", status["settings"])
        self.assertNotIn("watch_symbols", status["settings"])
        self.assertEqual(status["universe"], ["600000.SH"])
        self.assertEqual(status["execution_strategy"], "langchain")
        self.assertEqual(status["account_id"], "acct-legacy")
        self.assertEqual(status["model_id"], "model-legacy")

    async def test_build_task_duplicate_preset_appends_copy_suffix_without_legacy_signal_mode(self):
        service = self._build_service()
        created = await service.create_task(
            name="alpha",
            template_id="single-agent-trend",
            settings=self._inst(),
        )

        preset = await service.build_task_duplicate_preset(created.task_id)

        self.assertEqual(preset["name"], "alpha-copy")
        self.assertNotIn("signal_mode", preset)

    async def test_build_task_duplicate_preset_prefers_strategy_definition_binding(self):
        service = self._build_service()
        created = await service.create_task(
            name="definition-alpha",
            template_id="single-agent-trend",
            settings=self._inst(
                {
                    "strategy": {
                        "definition_id": "sd-demo",
                        "parameter_overrides": {"lookback": "20"},
                        "execution_profile": "default",
                    }
                }
            ),
        )

        preset = await service.build_task_duplicate_preset(created.task_id)

        self.assertEqual(preset["name"], "definition-alpha-copy")
        self.assertEqual(
            preset["strategy"],
            {
                "definition_id": "sd-demo",
                "parameter_overrides": {"lookback": "20"},
                "execution_profile": "default",
            },
        )
        self.assertNotIn("signal_mode", preset)
        self.assertNotIn("execution_strategy", preset)
        self.assertNotIn("account_id", preset)
        self.assertNotIn("model_id", preset)

    async def test_delete_task_removes_from_repository_and_scheduler(self):
        service = self._build_service()
        instance = await service.create_task(
            name="gone", template_id="single-agent-trend", settings=self._inst()
        )

        self.assertIn(instance.task_id, service.scheduler.tasks)

        await service.delete_task(instance.task_id)

        self.assertNotIn(instance.task_id, service.tasks)
        self.assertNotIn(instance.task_id, service.scheduler.tasks)

        listed = await service.list_tasks()
        self.assertEqual(listed, [])

    async def test_delete_tasks_deletes_multiple_non_running_tasks(self):
        service = self._build_service()
        first = await service.create_task(
            name="alpha", template_id="single-agent-trend", settings=self._inst()
        )
        second = await service.create_task(
            name="beta", template_id="single-agent-trend", settings=self._inst()
        )

        await service.delete_tasks([first.task_id, second.task_id])

        listed = await service.list_tasks()
        self.assertEqual(listed, [])

    async def test_create_task_allows_duplicate_names(self):
        service = self._build_service()

        first = await service.create_task(
            name="duplicate-name", template_id="single-agent-trend", settings=self._inst()
        )
        second = await service.create_task(
            name="duplicate-name", template_id="single-agent-trend", settings=self._inst()
        )

        listed = await service.list_tasks()

        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(first.config.name, "duplicate-name")
        self.assertEqual(second.config.name, "duplicate-name")
        self.assertEqual([item["name"] for item in listed], ["duplicate-name", "duplicate-name"])

    async def test_delete_tasks_rejects_running_tasks(self):
        service = self._build_service()
        running = await service.create_task(
            name="running", template_id="single-agent-trend", settings=self._inst()
        )
        stopped = await service.create_task(
            name="stopped", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(running.task_id)
        await service.stop_task(stopped.task_id)

        with self.assertRaisesRegex(RuntimeError, "running"):
            await service.delete_tasks([running.task_id, stopped.task_id])

        listed = await service.list_tasks()
        self.assertEqual(len(listed), 2)

    async def test_start_debug_session_rejects_running_task(self):
        service = self._build_service()
        instance = await service.create_task(
            name="omega", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)

        with self.assertRaises(RuntimeError):
            await service.start_debug_session(
                instance.task_id,
                input_overrides={"debug_note": "inspect"},
            )

    async def test_start_debug_session_rejects_backtest_task(self):
        service = self._build_service()
        instance = await service.create_task(
            name="omega-bt",
            mode="backtest",
            settings=self._inst(),
        )

        with self.assertRaisesRegex(RuntimeError, "backtest tasks do not support debug sessions"):
            await service.start_debug_session(
                instance.task_id,
                input_overrides={"debug_note": "inspect"},
            )

    async def test_start_debug_session_runs_background_worker_and_persists_session(self):
        seen_configs = []

        class _CapturingWorker(_CountingWorker):
            async def run_cycle(self, cycle_persist_context=None):
                seen_configs.append(self.config)
                return await super().run_cycle(cycle_persist_context=cycle_persist_context)

        service = self._build_service(worker_factory=lambda config, ms, acct=None: _CapturingWorker(config))
        instance = await service.create_task(
            name="psi",
            template_id="single-agent-trend",
            settings=self._inst(
                {
                    "watch_symbols": ["NVDA"],
                    "strategy_preferences": "lean momentum",
                    "react_max_turns": 3,
                    "signal_tool_names": ["data_bars_relative"],
                }
            ),
        )

        created = await service.start_debug_session(
            instance.task_id,
            input_overrides={
                "universe": ["NVDA"],
                "debug_note": "check single symbol",
            },
        )
        finished = await self._wait_for_debug_status(service, instance.task_id, created["session_id"])
        listed = await service.list_debug_sessions(instance.task_id)

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_id"], created["session_id"])
        self.assertIsInstance(finished["effective_config"], dict)
        self.assertNotIn("template_id", finished["effective_config"])
        self.assertNotIn("orchestrator_mode", finished["effective_config"])
        self.assertNotIn("watch_symbols", finished["effective_config"])
        self.assertEqual(seen_configs[0].react_max_turns, 3)

    async def test_start_debug_session_marks_failed_when_cycle_reports_signal_failure(self):
        service = self._build_service(worker_factory=lambda config, ms, acct=None: _FailedSignalCycleWorker(config))
        instance = await service.create_task(
            name="sigma", template_id="single-agent-trend", settings=self._inst()
        )

        created = await service.start_debug_session(
            instance.task_id,
            input_overrides={"debug_note": "expect failed"},
        )
        finished = await self._wait_for_debug_status(service, instance.task_id, created["session_id"])

        self.assertEqual(finished["status"], "failed")
        self.assertIn("signal blew up", finished["error_message"])

    async def test_start_debug_session_rejects_parallel_debug_runs(self):
        wait = asyncio.Event()

        class _BlockingWorker(_CountingWorker):
            async def run_cycle(self, cycle_persist_context=None):
                await wait.wait()
                return await super().run_cycle(cycle_persist_context=cycle_persist_context)

        service = self._build_service(worker_factory=lambda config, ms, acct=None: _BlockingWorker(config))
        instance = await service.create_task(
            name="chi", template_id="single-agent-trend", settings=self._inst()
        )

        first = await service.start_debug_session(
            instance.task_id,
            input_overrides={"debug_note": "first"},
        )
        try:
            with self.assertRaises(RuntimeError):
                await service.start_debug_session(
                    instance.task_id,
                    input_overrides={"debug_note": "second"},
                )
        finally:
            wait.set()
            await self._wait_for_debug_status(service, instance.task_id, first["session_id"])

    async def test_run_backtest_job_persists_summary_and_marks_task_completed(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-success",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        record = await self.task_repository.get_task(inst.task_id)
        self.assertEqual(record.status, "completed")
        self.assertIsInstance(record.backtest_summary, dict)
        summary = record.backtest_summary
        assert summary is not None
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["bar_interval"], "1d")
        self.assertEqual(summary["run_id"], worker.last_run_id)
        self.assertEqual(summary["data_provider"], "mock")
        self.assertEqual(summary["data_provider_effective"], "mock")
        self.assertEqual(summary["trade_count_closed"], 0)
        self.assertEqual(summary["trade_count_open"], 0)
        self.assertIn("equity_curve", summary)
        self.assertEqual(len(summary["equity_curve"]), 2)
        self.assertEqual(summary["equity_curve_meta"]["raw_length"], 2)
        self.assertFalse(summary["equity_curve_meta"]["downsampled"])
        for required in (
            "starting_equity",
            "ending_equity",
            "return_pct",
            "final_cash",
            "final_market_value",
            "win_rate",
            "avg_holding_trading_days",
            "max_drawdown_pct",
        ):
            self.assertIsInstance(summary[required], str, msg=required)

        row = await self.run_repository.get(job_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "completed")

    async def test_start_backtest_job_5m_counts_trading_days_not_intraday_bars(self):
        from doyoutrade.data.mock_provider import MockTradingDataProvider

        sym = "600000.SH"
        bars_provider = MockTradingDataProvider(
            bars_by_symbol={
                sym: [
                    _intraday_bar(sym, "2026-01-05T09:35:00", 10.0),
                    _intraday_bar(sym, "2026-01-05T09:40:00", 10.2),
                    _intraday_bar(sym, "2026-01-06T09:35:00", 10.5),
                    _intraday_bar(sym, "2026-01-06T09:40:00", 10.8),
                ]
            }
        )
        worker = _CycleContextRecordingBacktestWorker(data_provider=bars_provider)
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-5m-bars-total",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
            bar_interval="5m",
        )
        job_id = job["run_id"]
        row = await self.run_repository.get(job_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["bar_interval"], "5m")
        self.assertEqual(row["bars_total"], 4)

        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

    async def test_run_backtest_job_5m_uses_one_daily_cycle_time_per_trading_day(self):
        from doyoutrade.data.mock_provider import MockTradingDataProvider

        sym = "600000.SH"
        bars_provider = MockTradingDataProvider(
            bars_by_symbol={
                sym: [
                    _intraday_bar(sym, "2026-01-05T09:35:00", 10.0),
                    _intraday_bar(sym, "2026-01-05T09:40:00", 10.2),
                    _intraday_bar(sym, "2026-01-06T09:35:00", 10.5),
                    _intraday_bar(sym, "2026-01-06T09:40:00", 10.8),
                ]
            }
        )
        worker = _CycleContextRecordingBacktestWorker(data_provider=bars_provider)
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-5m-cycle-time",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
            bar_interval="5m",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        self.assertEqual(len(worker.cycle_contexts), 4)
        first_ctx, second_ctx, third_ctx, fourth_ctx = worker.cycle_contexts
        for ctx in worker.cycle_contexts:
            runtime_params = ctx.get("runtime_params") or {}
            input_overrides = runtime_params.get("input_overrides") or {}
            self.assertEqual(input_overrides.get("bar_interval"), "5m")
            self.assertEqual(ctx.get("run_kind"), "backtest_bar")

        self.assertEqual(first_ctx.get("cycle_time"), datetime(2026, 1, 5, 9, 35, 0))
        self.assertEqual(second_ctx.get("cycle_time"), datetime(2026, 1, 5, 9, 40, 0))
        self.assertEqual(third_ctx.get("cycle_time"), datetime(2026, 1, 6, 9, 35, 0))
        self.assertEqual(fourth_ctx.get("cycle_time"), datetime(2026, 1, 6, 9, 40, 0))

    async def test_persist_backtest_summary_uses_symbol_to_price_for_open_positions(self):
        """Open positions surfaced in ``backtest_summary.final_positions`` must carry
        ``last_price`` / ``market_value`` / ``weight_pct`` when only ``symbol_to_price``
        has the price (the typical end-of-run state with ``StoreBackedAccountReader``,
        which does not MTM ``PositionSnapshot.market_price``).
        """
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-fallback",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        end_position = PositionSnapshot(
            symbol="600522.SH",
            quantity=924.0,
            cost_price=Decimal("32.44"),
            available=924.0,
            market_price=None,
            market_value=None,
        )
        end_snap = AccountSnapshot(cash=Decimal("70025.44"), equity=Decimal("107900.20"))

        from doyoutrade.backtest import summary as backtest_summary

        await service._persist_backtest_summary(
            job_id="btjob-fallback-test",
            task_id=inst.task_id,
            run_id="run-fallback-test",
            range_start_utc=datetime(2026, 4, 16),
            range_end_utc=datetime(2026, 5, 15),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            end_snapshot=end_snap,
            end_positions=[end_position],
            equity_history=(
                backtest_summary.EquityPoint(t=datetime(2026, 4, 16), equity=Decimal("100000")),
                backtest_summary.EquityPoint(t=datetime(2026, 5, 15), equity=Decimal("107900.20")),
            ),
            fills=(),
            trading_dates=("2026-04-16", "2026-05-15"),
            status="completed",
            last_error=None,
            symbol_to_price={"600522.SH": "40.99"},
        )

        record = await self.task_repository.get_task(inst.task_id)
        summary = record.backtest_summary
        self.assertIsInstance(summary, dict)
        assert summary is not None
        final_positions = summary.get("final_positions") or []
        self.assertEqual(len(final_positions), 1)
        fp = final_positions[0]
        self.assertEqual(fp["symbol"], "600522.SH")
        self.assertEqual(Decimal(fp["last_price"]), Decimal("40.99"))
        self.assertEqual(
            Decimal(fp["market_value"]), Decimal("924") * Decimal("40.99")
        )
        self.assertIsNotNone(fp["weight_pct"])
        self.assertGreater(Decimal(fp["weight_pct"]), Decimal("0"))

    async def test_persist_backtest_summary_persists_warmup_fields_no_event(self):
        """``startup_history`` and ``bars_total`` are still persisted on
        the summary row for retrospective diagnostics, but the dedicated
        ``backtest_summary_warmup_insufficient`` debug event is no longer
        emitted: its predicate (``bars_total < startup_history``) mistook
        the user's report-window length for a preload failure, even
        though the SDK runner re-fetches ``startup_history`` bars per
        cycle via the cache's pre-warmup preload. The authoritative
        preload-failure signal is the runner's
        ``strategy_base_history_insufficient`` event, surfaced in the
        debug session rather than re-derived from the summary row.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)

        class _StubDefinition:
            definition_id = "defn-warmup"
            capabilities_json = {"startup_history": 50, "timeframe": "1d"}

        class _StubDefinitionRepo:
            async def get_definition(self, definition_id: str):
                return _StubDefinition()

        class _StubRuntime:
            definition_repository = _StubDefinitionRepo()

        service.strategy_runtime = _StubRuntime()

        inst = await service.create_task(
            name="bt-summary-warmup",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        end_snap = AccountSnapshot(cash=Decimal("100000"), equity=Decimal("100000"))

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        from doyoutrade.backtest import summary as backtest_summary

        # Patch the symbol the service module actually calls so we observe
        # the event without depending on the global OTel exporter.
        with patch(
            "doyoutrade.platform.service.emit_debug_event", _capture
        ):
            await service._persist_backtest_summary(
                job_id="btjob-warmup-test",
                task_id=inst.task_id,
                run_id="run-warmup-test",
                range_start_utc=datetime(2026, 4, 16),
                range_end_utc=datetime(2026, 5, 15),
                bar_interval="1d",
                starting_equity=Decimal("100000"),
                end_snapshot=end_snap,
                end_positions=[],
                equity_history=(
                    backtest_summary.EquityPoint(
                        t=datetime(2026, 4, 16), equity=Decimal("100000")
                    ),
                ),
                fills=(),
                # 19 trading dates → bars_total = 19 < startup_history=50.
                trading_dates=tuple(
                    f"2026-04-{d:02d}" for d in range(16, 30)
                ) + tuple(f"2026-05-{d:02d}" for d in range(1, 6)),
                status="completed",
                last_error=None,
                strategy_definition_id="defn-warmup",
            )

        record = await self.task_repository.get_task(inst.task_id)
        summary = record.backtest_summary
        self.assertIsInstance(summary, dict)
        assert summary is not None
        # Diagnostic fields still land on the persisted row …
        self.assertEqual(summary.get("startup_history"), 50)
        self.assertEqual(summary.get("bars_total"), 19)

        # … but no warmup_insufficient event fires from the broken predicate.
        warmup_events = [evt for evt, _ in captured_events if evt == "backtest_summary_warmup_insufficient"]
        self.assertEqual(
            warmup_events,
            [],
            msg=f"warmup_insufficient event must no longer fire, got {captured_events!r}",
        )

    async def test_persist_backtest_summary_no_warmup_event_when_runtime_absent(self):
        """When the runtime cannot resolve ``startup_history`` (e.g. no
        ``strategy_runtime`` configured, like the API server's read-only
        deployments) the warmup-insufficient event is NOT emitted. The
        anomaly check fails open rather than fabricating a confidently-
        wrong hint based on a missing field.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        # service.strategy_runtime is None by default.

        inst = await service.create_task(
            name="bt-summary-no-runtime",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        end_snap = AccountSnapshot(cash=Decimal("100000"), equity=Decimal("100000"))
        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        from doyoutrade.backtest import summary as backtest_summary

        with patch(
            "doyoutrade.platform.service.emit_debug_event", _capture
        ):
            await service._persist_backtest_summary(
                job_id="btjob-no-runtime",
                task_id=inst.task_id,
                run_id="run-no-runtime",
                range_start_utc=datetime(2026, 4, 16),
                range_end_utc=datetime(2026, 5, 15),
                bar_interval="1d",
                starting_equity=Decimal("100000"),
                end_snapshot=end_snap,
                end_positions=[],
                equity_history=(
                    backtest_summary.EquityPoint(
                        t=datetime(2026, 4, 16), equity=Decimal("100000")
                    ),
                ),
                fills=(),
                trading_dates=("2026-04-16", "2026-05-15"),
                status="completed",
                last_error=None,
                strategy_definition_id="sd-not-resolvable",
            )

        record = await self.task_repository.get_task(inst.task_id)
        summary = record.backtest_summary
        assert summary is not None
        # startup_history stays None when the lookup fails …
        self.assertIsNone(summary.get("startup_history"))
        # … and bars_total is still surfaced (computed from trading_dates).
        self.assertEqual(summary.get("bars_total"), 2)
        # No warmup event must fire.
        self.assertEqual(
            [evt for evt, _ in captured_events if evt == "backtest_summary_warmup_insufficient"],
            [],
        )

    async def test_resolve_startup_history_emits_event_when_definition_lookup_fails(self):
        """When the cache-preload path resolves startup_history and the
        definition repository raises, a structured
        ``backtest_startup_history_unresolved`` event must fire with the
        cause and a repair hint. This is the signal operators pivot on
        when a real backtest silently falls back to the legacy 21-day
        preload — previously it disappeared into a stdout warning.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)

        class _RaisingDefinitionRepo:
            async def get_definition(self, definition_id: str):
                raise RuntimeError(f"definition {definition_id} blew up")

        class _StubRuntime:
            definition_repository = _RaisingDefinitionRepo()

        service.strategy_runtime = _StubRuntime()

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        with patch("doyoutrade.platform.service.emit_debug_event", _capture):
            value = await service._resolve_strategy_startup_history(
                strategy_definition_id="sd-broken", emit_failure_event=True
            )

        self.assertIsNone(value)
        unresolved_events = [
            payload
            for evt, payload in captured_events
            if evt == "backtest_startup_history_unresolved"
        ]
        self.assertEqual(len(unresolved_events), 1)
        payload = unresolved_events[0]
        self.assertEqual(payload["reason"], "definition_lookup_failed")
        self.assertEqual(payload["strategy_definition_id"], "sd-broken")
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertIn("blew up", payload["exc_message"])
        self.assertIn("sd-...", payload["hint"])

    async def test_resolve_startup_history_emits_event_when_capabilities_missing(self):
        """The capability map can be malformed in several distinguishable
        ways. ``capabilities_missing`` vs ``startup_history_field_missing``
        vs ``startup_history_type_invalid`` should each route to a
        different ``reason`` so the operator can decide whether to
        re-run definition update or fix the SDK extractor.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)

        class _StubDefinitionEmptyCaps:
            definition_id = "defn-cap-missing"
            capabilities_json: dict = {}

        class _StubDefinitionBadField:
            definition_id = "defn-bad-field"
            capabilities_json = {"startup_history": "not-an-int"}

        class _StubDefinitionRepoEmpty:
            async def get_definition(self, definition_id: str):
                return _StubDefinitionEmptyCaps()

        class _StubDefinitionRepoBad:
            async def get_definition(self, definition_id: str):
                return _StubDefinitionBadField()

        class _StubRuntimeEmpty:
            definition_repository = _StubDefinitionRepoEmpty()

        class _StubRuntimeBad:
            definition_repository = _StubDefinitionRepoBad()

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        with patch("doyoutrade.platform.service.emit_debug_event", _capture):
            service.strategy_runtime = _StubRuntimeEmpty()
            v1 = await service._resolve_strategy_startup_history(
                strategy_definition_id="sd-empty-caps", emit_failure_event=True
            )
            service.strategy_runtime = _StubRuntimeBad()
            v2 = await service._resolve_strategy_startup_history(
                strategy_definition_id="sd-bad-field", emit_failure_event=True
            )

        self.assertIsNone(v1)
        self.assertIsNone(v2)
        reasons = [
            p["reason"]
            for evt, p in captured_events
            if evt == "backtest_startup_history_unresolved"
        ]
        self.assertEqual(reasons, ["capabilities_missing", "startup_history_type_invalid"])
        # Definition id is included so the operator can run
        # `strategy definition update <sd-...>` without further detective work.
        unresolved = [
            p for evt, p in captured_events if evt == "backtest_startup_history_unresolved"
        ]
        self.assertEqual(unresolved[0]["strategy_definition_id"], "defn-cap-missing")
        self.assertEqual(unresolved[1]["strategy_definition_id"], "defn-bad-field")
        self.assertEqual(unresolved[1]["raw_type"], "str")

    async def test_resolve_startup_history_no_event_when_definition_id_empty(self):
        """The "no strategy definition" case is legitimate (e.g. read-only
        API deployments, summary-only callers). It must NOT emit the
        unresolved event — otherwise the trace gets spammed with
        non-actionable rows.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        # No strategy_runtime configured — would resolve to None anyway,
        # but we also pass an empty definition_id to be explicit.
        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        with patch("doyoutrade.platform.service.emit_debug_event", _capture):
            value_empty = await service._resolve_strategy_startup_history(
                strategy_definition_id="", emit_failure_event=True
            )
            value_none = await service._resolve_strategy_startup_history(
                strategy_definition_id=None, emit_failure_event=True
            )

        self.assertIsNone(value_empty)
        self.assertIsNone(value_none)
        self.assertEqual(
            [evt for evt, _ in captured_events if evt == "backtest_startup_history_unresolved"],
            [],
        )

    async def test_resolve_startup_history_summary_path_does_not_emit(self):
        """The summary-compute path calls the resolver too (to record
        ``startup_history`` on the persisted summary row). It must NOT
        opt into the failure event — otherwise a single backtest would
        emit two identical unresolved events. The preload path is the
        single source of this signal.
        """
        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)

        class _RaisingDefinitionRepo:
            async def get_definition(self, definition_id: str):
                raise RuntimeError("boom")

        class _StubRuntime:
            definition_repository = _RaisingDefinitionRepo()

        service.strategy_runtime = _StubRuntime()

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        with patch("doyoutrade.platform.service.emit_debug_event", _capture):
            # Default kwarg (emit_failure_event=False).
            value = await service._resolve_strategy_startup_history(
                strategy_definition_id="sd-broken"
            )

        self.assertIsNone(value)
        self.assertEqual(
            [evt for evt, _ in captured_events if evt == "backtest_startup_history_unresolved"],
            [],
        )

    async def test_run_backtest_job_emits_summary_compute_otel_span(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-otel",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        run_row = await self.run_repository.get(job_id)
        assert run_row is not None
        session_id = str(run_row["session_id"])
        self.assertTrue(session_id)

        summary_span = None
        for _ in range(50):
            spans = await self.debug_session_span_repo.list_spans_for_session(session_id)
            for sp in spans:
                if sp.name == "backtest.summary.compute":
                    summary_span = sp
                    break
            if summary_span is not None:
                break
            await asyncio.sleep(0.02)

        self.assertIsNotNone(
            summary_span, msg="expected a backtest.summary.compute span to be exported"
        )
        assert summary_span is not None
        attrs = summary_span.attributes or {}
        self.assertEqual(attrs.get("run_id"), worker.last_run_id)
        self.assertEqual(attrs.get("task_id"), inst.task_id)
        self.assertEqual(attrs.get("bars_total"), 2)
        self.assertEqual(attrs.get("bars_completed"), 2)
        self.assertEqual(attrs.get("fills_count"), 0)
        self.assertEqual(attrs.get("closed_trades"), 0)
        self.assertEqual(attrs.get("open_trades"), 0)
        self.assertIn("max_drawdown_pct", attrs)
        self.assertEqual(summary_span.status, "ok")

    async def test_get_run_debug_view_accepts_backtest_job_run_id(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-debug-view-job",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        debug_view = await service.get_run_debug_view(job_id)

        self.assertEqual(debug_view["resolved_from"]["identifier"], job_id)
        self.assertEqual(debug_view["resolved_from"]["identifier_type"], "backtest_job")
        self.assertIsNotNone(debug_view["backtest_job"])
        self.assertEqual(debug_view["backtest_job"]["run_id"], job_id)
        self.assertIsNotNone(debug_view["session"])
        self.assertIsInstance(debug_view["cycle_runs"], list)
        self.assertGreater(len(debug_view["spans"]), 0)
        # ``signal_timeline`` is part of the contract: present as a list
        # (possibly empty), never omitted. The frontend reads it to render
        # per-bar signal_tag without recomputing indicators locally.
        self.assertIn("signal_timeline", debug_view)
        self.assertIsInstance(debug_view["signal_timeline"], list)

    async def test_get_run_debug_view_accepts_debug_session_id(self):
        service = self._build_service()
        instance = await service.create_task(
            name="debug-view-session",
            settings=self._inst(),
        )
        created = await service.start_debug_session(
            instance.task_id,
            input_overrides={"debug_note": "session-debug-view"},
        )
        finished = await self._wait_for_debug_status(service, instance.task_id, created["session_id"])

        debug_view = await service.get_run_debug_view(created["session_id"])

        self.assertEqual(debug_view["resolved_from"]["identifier"], created["session_id"])
        self.assertEqual(debug_view["resolved_from"]["identifier_type"], "debug_session")
        self.assertIsNotNone(debug_view["session"])
        self.assertEqual(debug_view["session"]["session_id"], created["session_id"])
        self.assertEqual(debug_view["session"]["run_id"], finished["run_id"])
        self.assertIsInstance(debug_view["cycle_runs"], list)

    async def test_get_run_debug_view_unknown_identifier_reports_supported_kinds(self):
        service = self._build_service()

        with self.assertRaisesRegex(
            Exception,
            "expected cycle run id, backtest job id, or debug session id",
        ):
            await service.get_run_debug_view("missing-debug-view-id")

    async def test_get_trace_debug_view_resolves_by_trace_id(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-trace-view-job",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        # Discover a real trace_id from the recorded spans (each cycle owns a trace).
        run_view = await service.get_run_debug_view(job_id)
        trace_id = next(
            (span.get("trace_id") for span in run_view["spans"] if span.get("trace_id")),
            None,
        )
        self.assertIsNotNone(trace_id, "backtest spans should carry a trace_id")

        trace_view = await service.get_trace_debug_view(trace_id)

        self.assertEqual(trace_view["resolved_from"]["identifier"], trace_id)
        self.assertEqual(trace_view["resolved_from"]["identifier_type"], "trace")
        # All returned spans are scoped to exactly the requested trace.
        self.assertGreater(len(trace_view["spans"]), 0)
        self.assertTrue(all(s.get("trace_id") == trace_id for s in trace_view["spans"]))
        self.assertIn("signal_timeline", trace_view)
        self.assertIsInstance(trace_view["signal_timeline"], list)
        self.assertIsInstance(trace_view["cycle_runs"], list)

    async def test_get_trace_debug_view_rejects_invalid_trace_id(self):
        service = self._build_service()

        with self.assertRaisesRegex(Exception, "invalid trace_id"):
            await service.get_trace_debug_view("not-a-hex-trace")

    async def test_get_trace_debug_view_unknown_trace_not_found(self):
        service = self._build_service()

        # Well-formed (32-char hex) but never recorded.
        with self.assertRaisesRegex(Exception, "debug view not found for trace_id"):
            await service.get_trace_debug_view("0" * 32)

    async def test_run_backtest_job_finalize_failure_persists_partial_summary(self):
        worker = _BacktestSummaryWorker(fail_at_cycle=1)
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-fail",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        record = await self.task_repository.get_task(inst.task_id)
        self.assertEqual(record.status, "error")
        self.assertIn("fail-cycle-1", record.last_error)
        self.assertIsInstance(record.backtest_summary, dict)
        summary = record.backtest_summary
        assert summary is not None
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["bar_interval"], "1d")
        self.assertEqual(summary["run_id"], worker.last_run_id)
        self.assertEqual(len(summary["equity_curve"]), 1)

        row = await self.run_repository.get(job_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "failed")

    async def test_get_backtest_summary_returns_ok_for_freshly_persisted_run(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-reader-ok",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        result = await service.get_backtest_summary(job_id)

        self.assertEqual(result["summary_state"], "ok")
        self.assertEqual(result["task_id"], inst.task_id)
        self.assertEqual(result["latest_summary_run_id"], job_id)
        self.assertIsNotNone(result["summary"])
        # Reader matches on backtest_job_id; legacy run_id keeps the
        # final cycle_run_id for back-compat consumers (frontend timelines).
        self.assertEqual(result["summary"]["backtest_job_id"], job_id)
        self.assertNotEqual(result["summary"]["run_id"], job_id)
        self.assertEqual(result["run"]["status"], "completed")

    async def test_get_backtest_summary_stale_when_persisted_summary_is_for_another_run(self):
        """Simulate the overwrite case by completing one backtest then writing
        a synthetic summary that points to a different job_id. The reader must
        report ``stale`` and surface the persisted ``backtest_job_id`` so the
        agent can recover with ``get_backtest_summary(latest_summary_run_id)``.
        """

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-summary-reader-stale",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        first_id = job["run_id"]
        bt_task = service.backtest_tasks.get(first_id)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        # Overwrite the persisted summary so it looks like it came from a
        # different (newer) backtest run on the same task. This avoids the
        # "task already has a run" guard, which is not the contract we're
        # exercising here.
        synthetic = await self.task_repository.get_task(inst.task_id)
        stale_summary = dict(synthetic.backtest_summary or {})
        stale_summary["backtest_job_id"] = "btjob-newer-overwrite"
        await self.task_repository.update_backtest_summary_and_status(
            inst.task_id,
            summary=stale_summary,
            status=synthetic.status,
            last_error=synthetic.last_error,
        )

        result = await service.get_backtest_summary(first_id)

        self.assertEqual(result["summary_state"], "stale")
        self.assertIsNone(result["summary"])
        self.assertEqual(result["latest_summary_run_id"], "btjob-newer-overwrite")
        self.assertEqual(result["run"]["run_id"], first_id)

    async def test_get_backtest_summary_raises_for_unknown_run_id(self):
        service = self._build_service()
        from doyoutrade.persistence.errors import RecordNotFoundError

        with self.assertRaises(RecordNotFoundError):
            await service.get_backtest_summary("btjob-does-not-exist")

    async def test_get_backtest_chart_trades_include_rationale(self):
        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-chart-rationale",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        inserted = await self.trade_fill_repository.insert_fill(
            task_id=inst.task_id,
            cycle_run_id=f"run-cycle-{uuid.uuid4()}",
            run_id=job_id,
            session_id=None,
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.2",
            amount="1020",
            fee=None,
            currency=None,
            intent_id="intent-rationale-1",
            rationale="factor.macd.golden_cross",
            filled_at=datetime(2026, 1, 5, 0, 0, 0),
            source_mode="backtest",
            raw_payload=None,
        )
        self.assertTrue(inserted)

        chart = await service.get_backtest_chart(inst.task_id, job_id, symbol="600000.SH")

        self.assertTrue(chart["trades"])
        self.assertEqual(chart["adjust"], "qfq")
        trade = chart["trades"][0]
        self.assertEqual(trade["intent_id"], "intent-rationale-1")
        self.assertEqual(trade["rationale"], "factor.macd.golden_cross")

    async def test_get_backtest_chart_prepends_indicator_warmup_bars(self):
        from doyoutrade.data.mock_provider import MockTradingDataProvider

        recording_provider = _RecordingBarsDataProvider(MockTradingDataProvider())
        worker = _BacktestSummaryWorker(data_provider=recording_provider)
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        inst = await service.create_task(
            name="bt-chart-warmup",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        bt_task = service.backtest_tasks.get(job_id)
        self.assertIsNotNone(bt_task)
        assert bt_task is not None
        await asyncio.wait_for(bt_task, timeout=5.0)

        inserted = await self.trade_fill_repository.insert_fill(
            task_id=inst.task_id,
            cycle_run_id=f"run-cycle-{uuid.uuid4()}",
            run_id=job_id,
            session_id=None,
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.2",
            amount="1020",
            fee=None,
            currency=None,
            intent_id="intent-warmup-1",
            rationale="factor.macd.golden_cross",
            filled_at=datetime(2026, 1, 5, 0, 0, 0),
            source_mode="backtest",
            raw_payload=None,
        )
        self.assertTrue(inserted)

        await service.get_backtest_chart(inst.task_id, job_id, symbol="600000.SH")

        self.assertTrue(recording_provider.get_bars_calls)
        _symbol, chart_start, chart_end, _interval, adjust = recording_provider.get_bars_calls[-1]
        self.assertLess(chart_start, "2026-01-05")
        self.assertEqual(chart_end, "2026-01-06")
        self.assertEqual(adjust, "qfq")

    async def test_start_debug_session_allowed_when_scheduled_tick_session_pending(self):
        """Scheduled ticks keep a long-lived ``scheduled-<id>`` row in pending; it must not block debug."""
        service = self._build_service()
        instance = await service.create_task(
            name="lambda", template_id="single-agent-trend", settings=self._inst()
        )
        await service.start_task(instance.task_id)
        await service.tick_once("scheduled")
        await service.stop_task(instance.task_id)

        created = await service.start_debug_session(
            instance.task_id,
            input_overrides={"debug_note": "after scheduled"},
        )
        self.assertTrue(str(created["session_id"]).startswith("debug-"))
        finished = await self._wait_for_debug_status(service, instance.task_id, created["session_id"])
        self.assertEqual(finished["status"], "completed")

    async def test_tick_once_with_source_cron_and_task_ids_targets_specific_task(self):
        """``source="cron"`` with ``task_ids`` only ticks the named tasks."""
        service = self._build_service()
        interval_inst = await service.create_task(
            name="interval-task",
            template_id="single-agent-trend",
            settings=self._inst(),
        )
        cron_inst = await service.create_task(
            name="cron-task",
            template_id="single-agent-trend",
            settings=self._inst(),
        )
        await service.start_task(interval_inst.task_id)
        await service.start_task(cron_inst.task_id)

        executed = await service.tick_once(source="cron", task_ids=[cron_inst.task_id])

        self.assertEqual(executed, 1)
        cron_status = await service.get_task_status(cron_inst.task_id)
        interval_status = await service.get_task_status(interval_inst.task_id)
        self.assertEqual(cron_status["cycles"], 1)
        self.assertEqual(interval_status["cycles"], 0)

    async def test_tick_once_with_source_cron_skips_non_running_task_ids(self):
        """``source="cron"`` still requires the named task to be running."""
        service = self._build_service()
        configured_inst = await service.create_task(
            name="configured-cron-task",
            template_id="single-agent-trend",
            settings=self._inst(),
        )
        running_inst = await service.create_task(
            name="running-cron-task",
            template_id="single-agent-trend",
            settings=self._inst(),
        )
        await service.start_task(running_inst.task_id)

        executed = await service.tick_once(
            source="cron",
            task_ids=[configured_inst.task_id, running_inst.task_id],
        )

        self.assertEqual(executed, 1)
        configured_status = await service.get_task_status(configured_inst.task_id)
        running_status = await service.get_task_status(running_inst.task_id)
        self.assertEqual(configured_status["cycles"], 0)
        self.assertEqual(running_status["cycles"], 1)

    async def test_tick_once_manual_ticks_running_instance(self):
        """``source="manual"`` ticks all running tasks (no tick_mode filter)."""
        service = self._build_service()
        inst = await service.create_task(
            name="manual-task",
            template_id="single-agent-trend",
            settings=self._inst(),
        )
        await service.start_task(inst.task_id)

        executed = await service.tick_once(source="manual")

        self.assertEqual(executed, 1)
        status = await service.get_task_status(inst.task_id)
        self.assertEqual(status["cycles"], 1)

    async def test_tick_once_rejects_unknown_source(self):
        """Unknown ``source`` values must raise ``ValueError``."""
        service = self._build_service()
        with self.assertRaises(ValueError):
            await service.tick_once(source="bogus")

    async def test_resolve_worker_account_paths(self):
        from doyoutrade.platform.service import AccountResolutionError
        from doyoutrade.runtime.cycle_task import CycleTaskConfig

        service = self._build_service()

        # live + no account → raise (never silently downgrade to mock)
        with self.assertRaises(AccountResolutionError):
            await service._resolve_worker_account(
                CycleTaskConfig(name="t", mode="live")
            )

        # backtest + no account → connectionless ResolvedAccount (baostock fallback)
        ra = await service._resolve_worker_account(
            CycleTaskConfig(name="t", mode="backtest")
        )
        self.assertEqual(ra.account_id, "")
        self.assertFalse(ra.has_connection)

        # default account is used when the task binds none
        d = await self.account_repo.upsert_account(
            {"name": "d", "mode": "live", "base_url": "http://x:9", "qmt_account_id": "a1"}
        )
        await self.account_repo.set_default(d["id"])
        ra2 = await service._resolve_worker_account(
            CycleTaskConfig(name="t", mode="live")
        )
        self.assertEqual(ra2.account_id, d["id"])
        self.assertEqual(ra2.qmt_account_id, "a1")

        # explicit binding wins over the default
        b = await self.account_repo.upsert_account(
            {"name": "b", "mode": "mock", "base_url": "http://y:9"}
        )
        ra3 = await service._resolve_worker_account(
            CycleTaskConfig(name="t", mode="live", account_id=b["id"])
        )
        self.assertEqual(ra3.account_id, b["id"])
        self.assertEqual(ra3.mode, "mock")

        # disabled bound account → raise account_disabled
        await self.account_repo.upsert_account({"id": b["id"], "enabled": False})
        with self.assertRaises(AccountResolutionError):
            await service._resolve_worker_account(
                CycleTaskConfig(name="t", mode="live", account_id=b["id"])
            )

    async def test_get_account_statement_uses_default_account_and_wraps_metadata(self):
        acct = await self.account_repo.upsert_account(
            {"name": "live-a", "mode": "live", "base_url": "http://localhost:5000",
             "qmt_account_id": "acc-123"}
        )
        await self.account_repo.set_default(acct["id"])
        service = self._build_service()

        class _Client:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        client = _Client()

        async def _fake_gather(reader, *, asof, captured_at, source):
            self.assertEqual(asof, date(2026, 6, 18))
            self.assertEqual(source, "broker")
            self.assertIsNotNone(reader)
            self.assertIsNotNone(captured_at)
            return {
                "asof": asof.isoformat(),
                "source": source,
                "account": {"account": {"cash": "1", "equity": "2"}, "positions": []},
                "asset": {"total_asset": "2"},
                "trades": [],
                "trade_count": 0,
                "errors": [],
            }

        with (
            patch("doyoutrade.infra.qmt.create_qmt_proxy_rest_client", return_value=client),
            patch("doyoutrade.account.statement.gather_account_statement", new=_fake_gather),
        ):
            statement = await service.get_account_statement(asof=date(2026, 6, 18))

        self.assertEqual(statement["account_id"], acct["id"])
        self.assertEqual(statement["account_name"], "live-a")
        self.assertEqual(statement["account_mode"], "live")
        self.assertTrue(statement["resolved_via_default"])
        self.assertTrue(client.closed)

    async def test_get_account_statement_rejects_missing_explicit_account(self):
        service = self._build_service()
        with self.assertRaises(KeyError):
            await service.get_account_statement("acct-missing", asof=date(2026, 6, 18))

    async def test_create_task_qmt_without_account_raises(self):
        """data_provider=qmt with no default/bound account (no base_url) → 400."""
        service = self._build_service()
        with self.assertRaisesRegex(ValueError, "requires an account with base_url"):
            await service.create_task(
                name="qmt-no-account",
                template_id="single-agent-trend",
                data_provider="qmt",
                settings=self._inst(),
            )

    async def test_create_task_qmt_with_default_account_succeeds(self):
        """A default account with base_url satisfies the qmt provider check."""
        acct = await self.account_repo.upsert_account(
            {"name": "live-a", "mode": "live", "base_url": "http://localhost:5000",
             "qmt_account_id": "acc-123"}
        )
        await self.account_repo.set_default(acct["id"])
        service = self._build_service()
        instance = await service.create_task(
            name="qmt-default-ok",
            template_id="single-agent-trend",
            data_provider="qmt",
            settings=self._inst(),
        )
        self.assertIsNotNone(instance.task_id)

    async def test_create_task_qmt_with_bound_account_succeeds(self):
        """An explicitly bound account (settings.account_id) is validated + used."""
        acct = await self.account_repo.upsert_account(
            {"name": "mock-a", "mode": "mock", "base_url": "http://localhost:5000"}
        )
        service = self._build_service()
        instance = await service.create_task(
            name="qmt-bound-ok",
            template_id="single-agent-trend",
            data_provider="qmt",
            settings=self._inst({"account_id": acct["id"]}),
        )
        self.assertIsNotNone(instance.task_id)

    async def test_create_task_bound_account_not_found_raises(self):
        """Binding a non-existent account_id is rejected at create time."""
        service = self._build_service()
        with self.assertRaisesRegex(ValueError, "account_not_found"):
            await service.create_task(
                name="bad-account",
                template_id="single-agent-trend",
                settings=self._inst({"account_id": "acct-doesnotexist"}),
            )

    async def test_list_tasks_summary_returns_only_required_fields(self):
        # Setup: create a task via the service
        svc = self._build_service()
        task_id = await svc.create_task(name="summary-test", template_id="single-agent-event", settings=self._inst())
        # Act
        result = await svc.list_tasks_summary(
            q=None,
            status=None,
            mode=None,
            definition_id=None,
            limit=20,
            offset=0,
        )
        # Assert structure
        self.assertIn("items", result)
        self.assertIn("total", result)
        self.assertIn("limit", result)
        self.assertIn("offset", result)
        self.assertGreaterEqual(result["total"], 1)
        item = result["items"][0]
        # Only summary fields present
        self.assertEqual(set(item.keys()), {"task_id", "name", "status", "mode", "last_error"})
        # No heavy fields
        self.assertNotIn("cycles", item)
        self.assertNotIn("settings", item)
        self.assertNotIn("backtest_summary", item)
        self.assertNotIn("watch_symbols", item)

    async def test_list_cycle_runs_summary_returns_only_required_fields(self):
        svc = self._build_service()
        task_id = await svc.create_task(name="cr-summary-test", template_id="single-agent-event", settings=self._inst())
        # Verify the summary method returns correct structure even with no cycle runs
        result = await svc.list_cycle_runs_summary(
            identifier=task_id,
            limit=50,
            offset=0,
            run_id_contains=None,
            status=None,
            run_kind=None,
            run_mode=None,
            started_after=None,
            started_before=None,
            run_id=None,
        )
        self.assertIn("items", result)
        self.assertIn("total", result)
        self.assertIn("limit", result)
        self.assertIn("offset", result)
        # Verify all items have only summary fields (empty list is ok)
        for item in result["items"]:
            self.assertEqual(
                set(item.keys()),
                {"run_id", "task_id", "status", "run_kind", "run_mode", "started_at"},
            )
            self.assertNotIn("agent_name", item)
            self.assertNotIn("session_id", item)
            self.assertNotIn("details", item)


class ParseCycleRunWallTimeRangeTests(unittest.TestCase):
    """``started_after > started_before`` must raise — without this, the
    SQL predicate built downstream is never satisfiable and the API
    silently returns ``{"items": [], "total": 0}``, leaving operators
    unable to tell whether their filter is wrong or the data really is
    empty (same "syntactically valid but semantically absurd input"
    shape as the cron next-fire-distance guard).
    """

    def test_reversed_range_raises_value_error(self):
        from doyoutrade.platform.service import _parse_cycle_run_wall_time_range
        with self.assertRaises(ValueError) as ctx:
            _parse_cycle_run_wall_time_range(
                "2026-05-24T00:00:00Z", "2026-05-23T00:00:00Z",
            )
        msg = str(ctx.exception)
        # Both bounds echoed so the caller can spot which side is wrong
        # without a round-trip.
        self.assertIn("started_after", msg)
        self.assertIn("must be <=", msg)
        self.assertIn("2026-05-24", msg)
        self.assertIn("2026-05-23", msg)

    def test_equal_bounds_accepted(self):
        """Equal bounds is a degenerate but valid range (matches the
        single instant). Must not raise."""
        from doyoutrade.platform.service import _parse_cycle_run_wall_time_range
        after, before = _parse_cycle_run_wall_time_range(
            "2026-05-23T00:00:00Z", "2026-05-23T00:00:00Z",
        )
        self.assertEqual(after, before)

    def test_normal_range_accepted(self):
        from doyoutrade.platform.service import _parse_cycle_run_wall_time_range
        after, before = _parse_cycle_run_wall_time_range(
            "2026-05-23T00:00:00Z", "2026-05-24T00:00:00Z",
        )
        self.assertIsNotNone(after)
        self.assertIsNotNone(before)
        self.assertLess(after, before)

    def test_one_sided_range_accepted(self):
        """Only one bound provided is a legitimate "open" query."""
        from doyoutrade.platform.service import _parse_cycle_run_wall_time_range
        after, before = _parse_cycle_run_wall_time_range(
            "2026-05-23T00:00:00Z", None,
        )
        self.assertIsNotNone(after)
        self.assertIsNone(before)

        after2, before2 = _parse_cycle_run_wall_time_range(
            None, "2026-05-24T00:00:00Z",
        )
        self.assertIsNone(after2)
        self.assertIsNotNone(before2)


class BacktestFinalPositionsTests(unittest.TestCase):
    """Cover the ``symbol_to_price`` fallback used to populate the open-position
    section of ``backtest_summary`` when ``PositionSnapshot.market_price`` is
    ``None`` (typical for ``StoreBackedAccountReader`` end-of-run snapshots)."""

    def test_falls_back_to_symbol_to_price_when_market_price_missing(self):
        from doyoutrade.platform.service import _backtest_final_positions

        pos = PositionSnapshot(
            symbol="600522.SH",
            quantity=924.0,
            cost_price=Decimal("32.44"),
            available=924.0,
            market_price=None,
            market_value=None,
        )
        out = _backtest_final_positions(
            [pos], symbol_to_price={"600522.SH": "40.99"}
        )

        self.assertEqual(len(out), 1)
        fp = out[0]
        self.assertEqual(fp.last_price, Decimal("40.99"))
        self.assertEqual(fp.market_value, Decimal("924") * Decimal("40.99"))

    def test_market_price_wins_over_symbol_to_price_fallback(self):
        from doyoutrade.platform.service import _backtest_final_positions

        pos = PositionSnapshot(
            symbol="600522.SH",
            quantity=100.0,
            cost_price=Decimal("32.44"),
            market_price=41.5,
        )
        out = _backtest_final_positions(
            [pos], symbol_to_price={"600522.SH": "40.99"}
        )

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].last_price, Decimal("41.5"))

    def test_returns_none_pricing_when_no_fallback_available(self):
        from doyoutrade.platform.service import _backtest_final_positions

        pos = PositionSnapshot(
            symbol="600522.SH",
            quantity=100.0,
            cost_price=Decimal("32.44"),
            market_price=None,
            market_value=None,
        )
        out = _backtest_final_positions([pos])

        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].last_price)
        self.assertIsNone(out[0].market_value)


class ChartBarsStartWithWarmupTests(unittest.IsolatedAsyncioTestCase):
    """Cover :func:`_chart_bars_start_with_warmup` legacy and
    ``startup_history``-aware branches. The chart helper is shared with
    the frontend candlestick render path, so the legacy 250-day default
    must keep working when no per-strategy hint is available, and the
    new branch must expand widely when the strategy declares a longer
    warmup.
    """

    async def test_legacy_mode_no_startup_history_uses_default_warmup(self) -> None:
        from datetime import date as _date

        from doyoutrade.platform.service import (
            BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS,
            _chart_bars_start_with_warmup,
        )

        captured_calendar_starts: list[str] = []

        class _DP:
            async def get_trading_dates(self, start: str, end: str) -> list[str]:
                captured_calendar_starts.append(start)
                # Synthetic dense trading-date list: every day in window.
                from datetime import date as __d, timedelta as __td

                s = __d.fromisoformat(start)
                e = __d.fromisoformat(end)
                out: list[str] = []
                cur = s
                while cur <= e:
                    out.append(cur.isoformat())
                    cur += __td(days=1)
                return out

        start = await _chart_bars_start_with_warmup(
            data_provider=_DP(),
            range_start=_date(2026, 5, 23),
        )
        # The function snaps to ``trading_dates[N - warmup]``; with a
        # synthetic dense list spanning ``[range_start - 3*warmup,
        # range_start]`` (3*warmup+1 entries), the chosen index lands
        # at ``range_start - (warmup - 1)``. For warmup=250 and
        # range_start=2026-05-23 → 2025-09-16.
        from datetime import timedelta as _td

        self.assertEqual(
            start,
            (_date(2026, 5, 23) - _td(days=BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS - 1)).isoformat(),
        )
        # And the calendar window the lookup spans is 3x the default
        # trading-day warmup (covers weekends/holidays). 250*3 = 750.
        self.assertEqual(
            captured_calendar_starts[-1],
            (_date(2026, 5, 23) - timedelta(days=BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS * 3)).isoformat(),
        )

    async def test_startup_history_expands_warmup_above_floor(self) -> None:
        from datetime import date as _date

        from doyoutrade.platform.service import _chart_bars_start_with_warmup

        class _DP:
            async def get_trading_dates(self, start: str, end: str) -> list[str]:
                from datetime import date as __d, timedelta as __td

                s = __d.fromisoformat(start)
                e = __d.fromisoformat(end)
                out: list[str] = []
                cur = s
                while cur <= e:
                    out.append(cur.isoformat())
                    cur += __td(days=1)
                return out

        # With a startup_history of 400 trading days, the formula yields
        # max(250, ceil(400 * 1.5) + 10) = max(250, 610) = 610 trading
        # days. With the synthetic dense list, the function snaps to
        # index ``N - 610`` which lands at ``range_start - 609 days``.
        start = await _chart_bars_start_with_warmup(
            data_provider=_DP(),
            range_start=_date(2027, 1, 1),
            startup_history=400,
        )
        from datetime import date as _d2, timedelta as _td2

        expected = (_d2(2027, 1, 1) - _td2(days=609)).isoformat()
        self.assertEqual(start, expected)

    async def test_startup_history_below_floor_keeps_default(self) -> None:
        from datetime import date as _date

        from doyoutrade.platform.service import _chart_bars_start_with_warmup

        class _DP:
            async def get_trading_dates(self, start: str, end: str) -> list[str]:
                from datetime import date as __d, timedelta as __td

                s = __d.fromisoformat(start)
                e = __d.fromisoformat(end)
                out: list[str] = []
                cur = s
                while cur <= e:
                    out.append(cur.isoformat())
                    cur += __td(days=1)
                return out

        # startup_history=50 → ceil(50*1.5)+10 = 75+10 = 85, well below
        # the 250-day floor → effective warmup stays 250. Snapping rule
        # is the same as the legacy-mode case: range_start - 249 days.
        start = await _chart_bars_start_with_warmup(
            data_provider=_DP(),
            range_start=_date(2026, 5, 23),
            startup_history=50,
        )
        from datetime import timedelta as _td

        from doyoutrade.platform.service import BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS

        self.assertEqual(
            start,
            (_date(2026, 5, 23) - _td(days=BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS - 1)).isoformat(),
        )


class BacktestPreloadWarmupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Asserts that ``_run_backtest_job_body`` resolves the strategy's
    ``startup_history`` and threads it into
    :func:`build_backtest_cached_data_provider`, expanding the cache
    preload while keeping the user's reporting window untouched.
    """

    async def asyncSetUp(self) -> None:
        configure_tracing(tracing_enabled=True)
        ensure_debug_span_export_processors()
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.task_repository = SqlAlchemyTaskRepository(self.session_factory)
        self.system_state_repository = SqlAlchemySystemStateRepository(self.session_factory)
        self.debug_session_repo = SqlAlchemyDebugSessionRepository(self.session_factory)
        self.debug_session_span_repo = SqlAlchemyDebugSessionSpanRepository(self.session_factory)
        self.run_repository = SqlAlchemyRunRepository(self.session_factory)
        self.trade_fill_repository = SqlAlchemyTradeFillRepository(self.session_factory)
        self.model_route_repo = SqlAlchemyModelRouteRepository(self.session_factory)
        self.tick_session_repo = _FakeTickSessionRepository(
            self.debug_session_repo, self.debug_session_span_repo, self.task_repository
        )

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _inst(self, extra=None):
        base = {
            "tick_seconds": 0.1,
            "react_max_turns": DEFAULT_REACT_MAX_TURNS,
            "signal_tool_names": list(DEFAULT_SIGNAL_TOOL_NAMES),
        }
        if extra:
            base.update(extra)
        return base

    def _build_service(self, worker_factory=None):
        from doyoutrade.config import get_config

        return TradingPlatformService(
            scheduler=RuntimeScheduler(),
            app_cfg=get_config(),
            worker_factory=worker_factory or (lambda config, ms, acct=None: _CountingWorker(config)),
            task_repository=self.task_repository,
            system_state_repository=self.system_state_repository,
            debug_session_repository=self.debug_session_repo,
            debug_session_span_repository=self.debug_session_span_repo,
            run_repository=self.run_repository,
            tick_session_repository=self.tick_session_repo,
            trade_fill_repository=self.trade_fill_repository,
            model_route_repository=self.model_route_repo,
        )

    async def test_backtest_preload_uses_resolved_startup_history(self) -> None:
        """When the strategy runtime resolves ``startup_history=50`` the
        backtest job body must pass it into the cache preload, expanding
        the left side beyond the legacy 21-day window. The reporting
        window stays bound to the user's [d0, d1] dates (``bars_total``
        equals the count of trading days in that window).
        """

        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)

        # Stub the resolver so we exercise the wiring without requiring
        # an actual strategy_definition_id on the task settings. The unit
        # tests for ``_resolve_strategy_startup_history`` itself live in
        # the existing ``test_persist_backtest_summary_*`` cases.
        # ``**_`` swallows the ``strategy_definition_id`` /
        # ``emit_failure_event`` kwargs the preload call site passes — the
        # stub doesn't need to react to them.
        async def _stub_resolve(**_):
            return 50

        service._resolve_strategy_startup_history = _stub_resolve  # type: ignore[method-assign]

        inst = await service.create_task(
            name="bt-runbody-warmup",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        # ``emit_debug_event`` is re-exported from both modules; patch
        # the one referenced inside cached_bars where the event fires.
        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", _capture
        ):
            job = await service.start_backtest_job(
                inst.task_id,
                range_start="2026-04-23",
                range_end="2026-05-23",
            )
            job_id = job["run_id"]
            bt_task = service.backtest_tasks.get(job_id)
            assert bt_task is not None
            await asyncio.wait_for(bt_task, timeout=10.0)

        # The new debug event fires with the expected breakdown.
        warmup_events = [
            payload
            for evt, payload in captured_events
            if evt == "backtest_cache_preload_with_warmup"
        ]
        self.assertEqual(
            len(warmup_events),
            1,
            msg=f"expected one preload-warmup event, got {captured_events!r}",
        )
        payload = warmup_events[0]
        self.assertEqual(payload["run_id"], job_id)
        self.assertEqual(payload["startup_history"], 50)
        # ceil(50 * 1.7) + 5 = 90 calendar days on the left.
        self.assertEqual(payload["computed_left_days"], 90)
        self.assertTrue(payload["warmup_applied"])
        # 2026-04-23 - 90 days = 2026-01-23.
        self.assertEqual(payload["preload_start"], "2026-01-23")
        # Right stays at the legacy 21 days.
        self.assertEqual(payload["preload_end"], "2026-06-13")

        # The reporting window stays bound to the user's [d0, d1].
        # ``bars_total`` in the persisted summary equals the count of
        # trading days in that window (NOT the expanded preload).
        record = await self.task_repository.get_task(inst.task_id)
        summary = record.backtest_summary
        self.assertIsNotNone(summary)
        assert summary is not None
        # The mock provider's trading-date list for 2026-04-23 → 2026-05-23
        # determines the count; assert the persisted range matches the user input.
        self.assertEqual(summary["range_start_utc"][:10], "2026-04-23")
        self.assertEqual(summary["range_end_utc"][:10], "2026-05-23")
        # Whatever count the mock provider reports, it must NOT include
        # the pre-d0 expansion (i.e. it must be <= ~23 trading days,
        # nowhere near the 90-day calendar window).
        self.assertLessEqual(summary["bars_total"], 24)

    async def test_backtest_preload_falls_back_when_runtime_absent(self) -> None:
        """When the task has no ``strategy_definition_id`` (and therefore
        no strategy to resolve startup_history from), the preload falls
        back to the legacy 21-day expansion silently. No warning fires
        — emitting one for every backtest-without-a-definition would
        spam the trace; the genuine "definition configured but resolution
        failed" path is covered by the dedicated unit tests on
        ``_resolve_strategy_startup_history(emit_failure_event=True)``.
        """

        from unittest.mock import patch

        worker = _BacktestSummaryWorker()
        service = self._build_service(worker_factory=lambda config, ms, acct=None: worker)
        # service.strategy_runtime is None by default; the task has no
        # strategy_definition_id either (see ``_inst()``).

        inst = await service.create_task(
            name="bt-runbody-no-runtime",
            mode="backtest",
            data_provider="mock",
            settings=self._inst(),
        )

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type: str, payload: dict) -> None:
            captured_events.append((event_type, dict(payload)))

        with patch("doyoutrade.data.cached_bars.emit_debug_event", _capture):
            job = await service.start_backtest_job(
                inst.task_id,
                range_start="2026-01-05",
                range_end="2026-01-09",
            )
            job_id = job["run_id"]
            bt_task = service.backtest_tasks.get(job_id)
            assert bt_task is not None
            await asyncio.wait_for(bt_task, timeout=10.0)

        warmup_events = [
            payload
            for evt, payload in captured_events
            if evt == "backtest_cache_preload_with_warmup"
        ]
        self.assertEqual(len(warmup_events), 1)
        payload = warmup_events[0]
        self.assertIsNone(payload["startup_history"])
        # Legacy 21-day expansion (no warmup applied).
        self.assertEqual(payload["computed_left_days"], 21)
        self.assertFalse(payload["warmup_applied"])

        # No ``backtest_startup_history_unresolved`` event fires for the
        # legitimate "no instance configured" case.
        self.assertEqual(
            [evt for evt, _ in captured_events if evt == "backtest_startup_history_unresolved"],
            [],
        )


class BacktestFillRecordParseTests(unittest.TestCase):
    """``_backtest_fill_record_from_details`` lifts exit_reason from the payload."""

    def _payload(self, **overrides):
        base = {
            "symbol": "600000.SH",
            "side": "sell",
            "quantity": 100,
            "price": "11.00",
            "timestamp": "2026-04-27T04:00:00Z",
            "intent_id": "oi-1",
            "cycle_run_id": "r1",
        }
        base.update(overrides)
        return base

    def test_exit_reason_parsed_from_payload(self):
        from doyoutrade.platform.service import _backtest_fill_record_from_details

        rec = _backtest_fill_record_from_details(
            self._payload(exit_reason="trailing_stop"), run_id_fallback="r1"
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec.exit_reason, "trailing_stop")

    def test_missing_exit_reason_is_none(self):
        from doyoutrade.platform.service import _backtest_fill_record_from_details

        rec = _backtest_fill_record_from_details(self._payload(), run_id_fallback="r1")
        self.assertIsNotNone(rec)
        self.assertIsNone(rec.exit_reason)

    def test_entry_and_exit_tags_lifted_from_payload(self):
        from doyoutrade.platform.service import _backtest_fill_record_from_details

        rec = _backtest_fill_record_from_details(
            self._payload(side="buy", entry_tag="ma_cross+rsi"),
            run_id_fallback="r1",
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec.entry_tag, "ma_cross+rsi")
        self.assertIsNone(rec.exit_tag)

    def test_tags_absent_are_none(self):
        from doyoutrade.platform.service import _backtest_fill_record_from_details

        rec = _backtest_fill_record_from_details(self._payload(), run_id_fallback="r1")
        self.assertIsNone(rec.entry_tag)
        self.assertIsNone(rec.exit_tag)


if __name__ == "__main__":
    unittest.main()
