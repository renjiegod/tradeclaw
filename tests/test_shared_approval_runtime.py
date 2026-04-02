import unittest

from tradeclaw.bootstrap import build_platform_runtime


class SharedApprovalRuntimeTests(unittest.TestCase):
    def test_live_instance_uses_shared_approval_queue(self):
        runtime = build_platform_runtime()
        service = runtime["service"]
        approval_gate = runtime["approval_gate"]

        instance = service.create_instance(name="live-alpha", template_id="single-agent-trend", mode="live")
        service.start_instance(instance.instance_id)
        service.tick_once()

        pending = approval_gate.list_pending()
        self.assertGreaterEqual(len(pending), 1)
        self.assertEqual(pending[0].mode, "live")


if __name__ == "__main__":
    unittest.main()
