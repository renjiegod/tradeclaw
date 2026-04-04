import asyncio
import io
import unittest

from tradeclaw.api.runtime_loop import RuntimeTickLoop
from tradeclaw.observability import initialize_observability, reset_observability


class _FakeService:
    def __init__(self):
        self.calls = 0

    async def tick_once(self):
        self.calls += 1
        return 1


class _FakeApprovalGate:
    def __init__(self):
        self.calls = 0

    async def expire_pending(self):
        self.calls += 1
        return []


class RuntimeTickLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_observability()

    def tearDown(self):
        reset_observability()

    async def test_runtime_loop_runs_async_ticks_until_stopped(self):
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream)
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        loop = RuntimeTickLoop(service=service, approval_gate=approval_gate, interval_seconds=0.01)

        loop.start()
        await asyncio.sleep(0.035)
        await loop.stop()

        self.assertGreaterEqual(service.calls, 1)
        self.assertGreaterEqual(approval_gate.calls, 1)
        output = stream.getvalue()
        self.assertIn("runtime tick completed", output)
        self.assertIn("trace_id=", output)
        self.assertIn("span_id=", output)
        self.assertNotIn("trace_id=-", output)
        self.assertNotIn("span_id=-", output)
