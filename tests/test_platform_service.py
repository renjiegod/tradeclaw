import unittest

from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _CountingWorker:
    def __init__(self):
        self.cycles = 0

    def run_cycle(self):
        self.cycles += 1


class PlatformServiceTests(unittest.TestCase):
    def test_create_start_and_stop_instance(self):
        scheduler = RuntimeScheduler()
        service = TradingPlatformService(
            scheduler=scheduler,
            worker_factory=lambda config: _CountingWorker(),
        )

        instance = service.create_instance(name="alpha", template_id="single-agent-trend")
        service.start_instance(instance.instance_id)
        service.tick_once()

        status = service.get_instance_status(instance.instance_id)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["cycles"], 1)

        service.stop_instance(instance.instance_id)
        status_after_stop = service.get_instance_status(instance.instance_id)
        self.assertEqual(status_after_stop["status"], "stopped")

    def test_kill_switch_blocks_start(self):
        scheduler = RuntimeScheduler()
        service = TradingPlatformService(
            scheduler=scheduler,
            worker_factory=lambda config: _CountingWorker(),
        )

        instance = service.create_instance(name="beta", template_id="single-agent-trend")
        service.set_kill_switch(True)

        with self.assertRaises(RuntimeError):
            service.start_instance(instance.instance_id)


if __name__ == "__main__":
    unittest.main()
