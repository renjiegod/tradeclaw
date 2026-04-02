import asyncio
import unittest

from tradeclaw.api.runtime_loop import RuntimeTickLoop


class _FakeService:
    def __init__(self):
        self.calls = 0

    async def tick_once(self):
        self.calls += 1
        return 1


class _FakeApprovalGate:
    def __init__(self):
        self.calls = 0

    def expire_pending(self):
        self.calls += 1
        return []


class RuntimeTickLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_loop_runs_async_ticks_until_stopped(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        loop = RuntimeTickLoop(service=service, approval_gate=approval_gate, interval_seconds=0.01)

        loop.start()
        await asyncio.sleep(0.035)
        await loop.stop()

        self.assertGreaterEqual(service.calls, 1)
        self.assertGreaterEqual(approval_gate.calls, 1)

