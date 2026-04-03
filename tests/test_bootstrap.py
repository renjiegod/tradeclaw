import unittest
from pathlib import Path
import tempfile

from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import load_config


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_runtime_and_run_single_tick(self):
        runtime = build_platform_runtime()
        service = runtime["service"]

        instance = service.create_instance(name="demo", template_id="single-agent-trend")
        service.start_instance(instance.instance_id)
        executed = await service.tick_once()

        self.assertEqual(executed, 1)
        status = service.get_instance_status(instance.instance_id)
        self.assertEqual(status["status"], "running")

    async def test_runtime_fails_fast_when_model_config_is_invalid(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(
                """
model:
  provider: anthropic
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            with self.assertRaisesRegex(ValueError, "api_key"):
                build_platform_runtime(app_cfg=cfg)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
