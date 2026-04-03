import io
import unittest

from fastapi.testclient import TestClient

from tradeclaw.api.app import create_app
from tradeclaw.observability import initialize_observability, reset_observability


class _FakeService:
    def __init__(self):
        self.tick_calls = 0

    async def tick_once(self):
        self.tick_calls += 1
        return 2

    def list_instances(self):
        return []

    def list_templates(self):
        return []

    def get_system_state(self):
        return {"kill_switch_enabled": False, "instance_count": 0, "running_count": 0}

    def set_kill_switch(self, enabled):
        return None


class _FakeApprovalGate:
    def expire_pending(self):
        return ["expired-1"]

    def list_pending(self):
        return []


class ApiAppTests(unittest.TestCase):
    def tearDown(self):
        reset_observability()

    def test_tick_endpoint_awaits_async_service(self):
        app = create_app(_FakeService(), _FakeApprovalGate())
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream, app=app)

        with TestClient(app) as client:
            response = client.post("/system/tick")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"executed": 2, "expired_count": 1})
        lines = [line for line in stream.getvalue().splitlines() if "api system tick handled" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("trace_id=", lines[0])
        self.assertIn("span_id=", lines[0])
        self.assertNotIn("trace_id=-", lines[0])
        self.assertNotIn("span_id=-", lines[0])
