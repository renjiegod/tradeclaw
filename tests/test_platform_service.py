import tempfile
import unittest
from pathlib import Path

from tradeclaw.persistence.db import create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.models import Base
from tradeclaw.persistence.repositories import (
    SqlAlchemyInstanceRepository,
    SqlAlchemySystemStateRepository,
)
from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _CountingWorker:
    def __init__(self):
        self.cycles = 0

    async def run_cycle(self):
        self.cycles += 1


class _FailingWorker:
    def __init__(self, message: str):
        self.cycles = 0
        self._message = message

    async def run_cycle(self):
        raise RuntimeError(self._message)


class PlatformServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.instance_repository = SqlAlchemyInstanceRepository(self.session_factory)
        self.system_state_repository = SqlAlchemySystemStateRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _build_service(self, worker_factory=None):
        return TradingPlatformService(
            scheduler=RuntimeScheduler(),
            worker_factory=worker_factory or (lambda config: _CountingWorker()),
            instance_repository=self.instance_repository,
            system_state_repository=self.system_state_repository,
        )

    async def test_create_start_and_stop_instance(self):
        service = self._build_service()

        instance = await service.create_instance(name="alpha", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)
        await service.tick_once()

        status = await service.get_instance_status(instance.instance_id)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["cycles"], 1)

        await service.stop_instance(instance.instance_id)
        status_after_stop = await service.get_instance_status(instance.instance_id)
        self.assertEqual(status_after_stop["status"], "stopped")

    async def test_recover_running_instances_from_repository(self):
        service = self._build_service()
        instance = await service.create_instance(name="alpha", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)

        restored_service = self._build_service()
        recovered = await restored_service.restore_instances()

        self.assertEqual(recovered, 1)
        status = await restored_service.get_instance_status("alpha")
        self.assertEqual(status["status"], "running")

    async def test_kill_switch_blocks_restore_of_running_instances(self):
        service = self._build_service()
        instance = await service.create_instance(name="beta", template_id="single-agent-trend")
        await self.system_state_repository.set_kill_switch_enabled(True)
        await self.instance_repository.update_status(instance.instance_id, "running", "")

        restored_service = self._build_service()
        recovered = await restored_service.restore_instances()

        self.assertEqual(recovered, 0)
        status = await restored_service.get_instance_status("beta")
        self.assertEqual(status["status"], "running")
        system_state = await restored_service.get_system_state()
        self.assertTrue(system_state["kill_switch_enabled"])

    async def test_tick_once_honors_persisted_kill_switch_changes(self):
        service = self._build_service()
        instance = await service.create_instance(name="epsilon", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)
        await self.system_state_repository.set_kill_switch_enabled(True)

        executed = await service.tick_once()
        status = await service.get_instance_status(instance.instance_id)

        self.assertEqual(executed, 0)
        self.assertEqual(status["cycles"], 0)
        self.assertEqual(status["status"], "running")

    async def test_set_kill_switch_stops_running_instances_in_repository_state(self):
        service = self._build_service()
        instance = await service.create_instance(name="zeta", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)

        await service.set_kill_switch(True)

        status = await service.get_instance_status(instance.instance_id)
        system_state = await service.get_system_state()
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(system_state["running_count"], 0)
        self.assertTrue(system_state["kill_switch_enabled"])

    async def test_restore_failure_marks_instance_error(self):
        service = self._build_service()
        instance = await service.create_instance(name="gamma", template_id="single-agent-trend")
        await self.instance_repository.update_status(instance.instance_id, "running", "")

        restored_service = self._build_service(
            worker_factory=lambda config: (_ for _ in ()).throw(RuntimeError("restore failed"))
        )
        recovered = await restored_service.restore_instances()

        self.assertEqual(recovered, 0)
        status = await restored_service.get_instance_status("gamma")
        self.assertEqual(status["status"], "error")
        self.assertIn("restore failed", status["last_error"])

    async def test_scheduler_error_persists_to_repository(self):
        service = self._build_service(worker_factory=lambda config: _FailingWorker("tick failed"))
        instance = await service.create_instance(name="delta", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)

        executed = await service.tick_once()

        self.assertEqual(executed, 0)
        status = await service.get_instance_status("delta")
        self.assertEqual(status["status"], "error")
        self.assertIn("tick failed", status["last_error"])

    async def test_get_instance_status_returns_all_instance_business_fields(self):
        service = self._build_service()

        instance = await service.create_instance(
            name="theta",
            template_id="single-agent-event",
            mode="live",
            orchestrator_mode="multi-role",
            description="swing trader",
            data_provider="mock",
            watch_symbols=["AAPL", "MSFT"],
            execution_strategy="langchain",
            account_id="acct-9",
            model_id="gpt-4.1",
            settings={"risk": "medium"},
        )

        status = await service.get_instance_status(instance.instance_id)

        self.assertEqual(status["instance_id"], instance.instance_id)
        self.assertEqual(status["name"], "theta")
        self.assertEqual(status["template_id"], "single-agent-event")
        self.assertEqual(status["mode"], "live")
        self.assertEqual(status["orchestrator_mode"], "multi-role")
        self.assertEqual(status["description"], "swing trader")
        self.assertEqual(status["data_provider"], "mock")
        self.assertEqual(status["data_provider_effective"], "mock")
        self.assertEqual(status["watch_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(status["execution_strategy"], "langchain")
        self.assertEqual(status["account_id"], "acct-9")
        self.assertEqual(status["model_id"], "gpt-4.1")
        self.assertEqual(status["settings"], {"risk": "medium"})
        self.assertTrue(status["created_at"])
        self.assertTrue(status["updated_at"])


if __name__ == "__main__":
    unittest.main()
