import tempfile
import unittest
from pathlib import Path

from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import load_config
from tradeclaw.observability import reset_observability


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        reset_observability()

    async def test_build_runtime_and_run_single_tick(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
data:
  default_provider: mock
model:
  provider: demo
database:
  url: sqlite+aiosqlite:///{db_path}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            runtime = await build_platform_runtime(app_cfg=cfg)
            service = runtime["service"]

            instance = await service.create_instance(name="demo", template_id="single-agent-trend")
            await service.start_instance(instance.instance_id)
            executed = await service.tick_once()

            self.assertEqual(executed, 1)
            status = await service.get_instance_status(instance.instance_id)
            self.assertEqual(status["status"], "running")

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

    async def test_runtime_restores_running_instances_on_rebuild(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
data:
  default_provider: mock
model:
  provider: demo
database:
  url: sqlite+aiosqlite:///{db_path}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            runtime = await build_platform_runtime(app_cfg=cfg)
            service = runtime["service"]
            instance = await service.create_instance(name="demo", template_id="single-agent-trend")
            await service.start_instance(instance.instance_id)

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

            rebuilt = await build_platform_runtime(app_cfg=cfg)
            rebuilt_service = rebuilt["service"]
            executed = await rebuilt_service.tick_once()
            status = await rebuilt_service.get_instance_status("demo")

            self.assertEqual(executed, 1)
            self.assertEqual(status["status"], "running")

            rebuilt_close = rebuilt.get("aclose")
            if rebuilt_close is not None:
                await rebuilt_close()

    async def test_runtime_fails_fast_when_model_config_is_invalid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
model:
  provider: anthropic
database:
  url: sqlite+aiosqlite:///{db_path}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            with self.assertRaisesRegex(ValueError, "api_key"):
                await build_platform_runtime(app_cfg=cfg)


if __name__ == "__main__":
    unittest.main()
