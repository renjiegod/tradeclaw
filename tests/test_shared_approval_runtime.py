import unittest
import tempfile
from pathlib import Path

from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import load_config


class SharedApprovalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_instance_uses_shared_approval_queue(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(
                """
data:
  default_provider: mock
model:
  provider: demo
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            runtime = build_platform_runtime(app_cfg=cfg)
            service = runtime["service"]
            approval_gate = runtime["approval_gate"]

            instance = service.create_instance(name="live-alpha", template_id="single-agent-trend", mode="live")
            service.start_instance(instance.instance_id)
            await service.tick_once()

            pending = approval_gate.list_pending()
            self.assertGreaterEqual(len(pending), 1)
            self.assertEqual(pending[0].mode, "live")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
