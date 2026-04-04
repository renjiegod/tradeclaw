import unittest
import tempfile
from pathlib import Path

from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import load_config


class SharedApprovalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_instance_uses_shared_approval_queue(self):
        db_tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_tf.close()
        db_path = Path(db_tf.name)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(
                f"""
data:
  default_provider: mock
model:
  provider: demo
database:
  url: "sqlite+aiosqlite:///{db_path.as_posix()}"
""".strip()
            )
            path = Path(handle.name)
        runtime = None
        try:
            cfg = load_config(path)
            runtime = await build_platform_runtime(app_cfg=cfg)
            service = runtime["service"]
            approval_gate = runtime["approval_gate"]

            instance = await service.create_instance(
                name="live-alpha",
                template_id="single-agent-trend",
                mode="live",
            )
            await service.start_instance(instance.instance_id)
            await service.tick_once()

            pending = await approval_gate.list_pending()
            self.assertGreaterEqual(len(pending), 1)
            self.assertEqual(pending[0].mode, "live")
        finally:
            if runtime is not None:
                aclose = runtime.get("aclose")
                if aclose is not None:
                    await aclose()
            path.unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
