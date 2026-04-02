import unittest

from tradeclaw.runtime.instance import AgentInstance, AgentInstanceConfig
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _CountingWorker:
    def __init__(self):
        self.cycles = 0

    async def run_cycle(self):
        self.cycles += 1


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_runs_only_running_instances(self):
        scheduler = RuntimeScheduler()
        worker_a = _CountingWorker()
        worker_b = _CountingWorker()

        inst_a = AgentInstance(config=AgentInstanceConfig(name="a", mode="paper"), worker=worker_a)
        inst_b = AgentInstance(config=AgentInstanceConfig(name="b", mode="paper"), worker=worker_b)

        scheduler.register(inst_a)
        scheduler.register(inst_b)

        scheduler.start(inst_a.instance_id)
        await scheduler.tick_once()

        self.assertEqual(worker_a.cycles, 1)
        self.assertEqual(worker_b.cycles, 0)

        scheduler.start(inst_b.instance_id)
        await scheduler.tick_once()

        self.assertEqual(worker_a.cycles, 2)
        self.assertEqual(worker_b.cycles, 1)


if __name__ == "__main__":
    unittest.main()
