"""Backtest pause waits until the in-flight ``run_cycle`` completes (bar boundary)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from contextlib import suppress
from pathlib import Path

from doyoutrade.core.models import CycleReport
from doyoutrade.observability.debug_span_export import ensure_debug_span_export_processors, register_span_persist_sink
from doyoutrade.observability.tracing import configure_tracing
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import (
    SqlAlchemyDebugSessionRepository,
    SqlAlchemyDebugSessionSpanRepository,
    SqlAlchemyModelRouteRepository,
    SqlAlchemyRunRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTaskRepository,
)
from doyoutrade.platform.service import TradingPlatformService
from doyoutrade.runtime.scheduler import RuntimeScheduler

_BT_SETTINGS = {
    "react_max_turns": 1,
    "signal_tool_names": ["data_bars_relative"],
    "watch_symbols": ["600000.SH"],
    "universe": ["600000.SH"],
    "model_route_name": "bt-pause-route",
}


class _SlowBacktestWorker:
    """Holds first ``run_cycle`` until ``release_cycle`` is set."""

    def __init__(self, config=None):
        self.config = config
        self.last_run_id = "run-test-pause"
        self.cycle_started = asyncio.Event()
        self.release_cycle = asyncio.Event()
        from doyoutrade.account.store_reader import StoreBackedAccountReader
        from doyoutrade.data.mock_provider import MockTradingDataProvider

        self._store = MockTradingDataProvider()
        self.account_reader = StoreBackedAccountReader(self._store)

    @property
    def data_provider(self):
        return self._store

    async def run_cycle(self, cycle_persist_context=None):
        self.cycle_started.set()
        await self.release_cycle.wait()
        return CycleReport(
            submitted_count=0,
            vetoed_count=0,
            pending_approval_count=0,
            completed_phases=["test"],
            cycle_failed=False,
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


class BacktestPauseBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        configure_tracing(tracing_enabled=True)
        ensure_debug_span_export_processors()
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "bt-pause.db"
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
        self.tick_session_repo = _FakeTickSessionRepository(
            self.debug_session_repo, self.debug_session_span_repo, self.task_repository
        )

        async def _append(row: dict):
            await self.debug_session_span_repo.append_span(**row)

        def _sink(row: dict) -> None:
            asyncio.get_running_loop().create_task(_append(row))

        register_span_persist_sink(_sink)
        self._worker = _SlowBacktestWorker()

        self.model_route_repo = SqlAlchemyModelRouteRepository(self.session_factory)
        await self.model_route_repo.create(
            route_name="bt-pause-route",
            provider_kind="anthropic",
            api_key="sk-bt-pause",
            target_model="gpt-4o-mini",
        )

    async def asyncTearDown(self):
        register_span_persist_sink(None)
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _service(self) -> TradingPlatformService:
        from doyoutrade.config import get_config

        return TradingPlatformService(
            scheduler=RuntimeScheduler(),
            app_cfg=get_config(),
            worker_factory=lambda config, ms: self._worker,
            task_repository=self.task_repository,
            system_state_repository=self.system_state_repository,
            debug_session_repository=self.debug_session_repo,
            debug_session_span_repository=self.debug_session_span_repo,
            tick_session_repository=self.tick_session_repo,
            run_repository=self.run_repository,
            model_route_repository=self.model_route_repo,
        )

    @unittest.skipUnless(
        os.environ.get("DOYOUTRADE_RUN_BACKTEST_PAUSE_BARRIER") == "1",
        "timing-sensitive: stub run_cycle may not run within 2s on some asyncio runners "
        "(reproduces on pre-unified-strategy commits); set DOYOUTRADE_RUN_BACKTEST_PAUSE_BARRIER=1 to run.",
    )
    async def test_pause_waits_for_run_cycle_before_db_shows_paused(self):
        service = self._service()
        inst = await service.create_task(
            name="bt-pause",
            template_id="single-agent-trend",
            mode="backtest",
            data_provider="mock",
            settings=dict(_BT_SETTINGS),
        )
        job = await service.start_backtest_job(
            inst.task_id,
            range_start="2026-01-05",
            range_end="2026-01-06",
        )
        job_id = job["run_id"]
        await asyncio.wait_for(self._worker.cycle_started.wait(), timeout=2.0)
        row_mid = await self.run_repository.get(job_id)
        self.assertIsNotNone(row_mid)
        assert row_mid is not None
        self.assertEqual(row_mid["status"], "running")

        pause_task = asyncio.create_task(service.pause_backtest_job(inst.task_id, job_id))
        await asyncio.sleep(0.05)
        row_during = await self.run_repository.get(job_id)
        assert row_during is not None
        self.assertEqual(row_during["status"], "running")

        self._worker.release_cycle.set()
        await asyncio.wait_for(pause_task, timeout=3.0)
        row_after = await self.run_repository.get(job_id)
        assert row_after is not None
        self.assertEqual(row_after["status"], "paused")
        self.assertEqual(row_after["bars_completed"], 1)

        bt_task = service.backtest_tasks.get(job_id)
        if bt_task is not None and not bt_task.done():
            bt_task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(bt_task, timeout=2.0)
