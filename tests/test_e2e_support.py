import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.e2e.support import E2EModelMode, E2ERuntimeManager


class E2ESupportTests(unittest.TestCase):
    def test_runtime_manager_deletes_created_tasks_on_exit(self) -> None:
        async def _run() -> None:
            manager = E2ERuntimeManager(profile="isolated", model_mode=E2EModelMode.STUB)
            delete_task = AsyncMock()
            service = SimpleNamespace(delete_task=delete_task, instrument_catalog_repository=SimpleNamespace(upsert_rows=AsyncMock()))
            runtime = {"service": service, "aclose": AsyncMock()}
            bundle = SimpleNamespace(
                app_config=SimpleNamespace(),
                root_config_path=Path("/tmp/config.yaml"),
                merged_config_path=Path("/tmp/config.yaml"),
                e2e_settings={},
            )

            with (
                patch("tests.e2e.support.tempfile.TemporaryDirectory"),
                patch("tests.e2e.support.load_e2e_config", return_value=bundle),
                patch("tests.e2e.support.build_platform_runtime", AsyncMock(return_value=runtime)),
                patch("tests.e2e.support.seed_e2e_instrument_catalog", AsyncMock()),
                patch("tests.e2e.support.ensure_e2e_model_route", AsyncMock(return_value="route-test")),
            ):
                ctx = await manager.__aenter__()
                ctx.created_task_ids.update({"task-a", "task-b"})
                await manager.__aexit__(None, None, None)

            delete_task.assert_any_await("task-a")
            delete_task.assert_any_await("task-b")
            self.assertEqual(delete_task.await_count, 2)

        asyncio.run(_run())
