import unittest

from tradeclaw.channels.manager import ChannelManager
from tradeclaw.execution.approval import QueuedApprovalGate
from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _NoopWorker:
    def run_cycle(self):
        return None


class ChannelManagerTests(unittest.TestCase):
    def test_start_and_status_commands(self):
        service = TradingPlatformService(
            scheduler=RuntimeScheduler(),
            worker_factory=lambda config: _NoopWorker(),
        )
        instance = service.create_instance(name="gamma", template_id="single-agent-trend")
        manager = ChannelManager(service=service, approval_gate=QueuedApprovalGate())

        start_resp = manager.handle_command(f"/start {instance.instance_id}")
        status_resp = manager.handle_command(f"/status {instance.instance_id}")

        self.assertIn("running", start_resp)
        self.assertIn("running", status_resp)

    def test_kill_command_toggles_kill_switch(self):
        service = TradingPlatformService(
            scheduler=RuntimeScheduler(),
            worker_factory=lambda config: _NoopWorker(),
        )
        manager = ChannelManager(service=service, approval_gate=QueuedApprovalGate())

        resp = manager.handle_command("/kill")

        self.assertIn("enabled", resp)
        self.assertTrue(service.kill_switch_enabled)


if __name__ == "__main__":
    unittest.main()
