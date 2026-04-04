import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from tradeclaw.api.app import create_app
from tradeclaw.api.server import build_api_with_runtime
from tradeclaw.observability import initialize_observability, reset_observability


class _FakeService:
    def __init__(self):
        self.tick_calls = 0
        self.kill_switch_calls = []
        self.create_calls = []
        self.instances = {}

    async def tick_once(self):
        self.tick_calls += 1
        return 2

    async def list_instances(self):
        return list(self.instances.values()) or [{"instance_id": "i-1", "status": "running"}]

    def list_templates(self):
        return []

    async def get_system_state(self):
        return {"kill_switch_enabled": False, "instance_count": 1, "running_count": 1}

    async def set_kill_switch(self, enabled):
        self.kill_switch_calls.append(enabled)

    async def create_instance(self, **payload):
        self.create_calls.append(payload)
        instance_id = f"instance-{len(self.create_calls)}"
        mode = payload.get("mode") or "paper"
        record = {
            "instance_id": instance_id,
            "name": payload["name"],
            "template_id": payload["template_id"],
            "mode": mode,
            "orchestrator_mode": payload.get("orchestrator_mode") or "single-agent",
            "description": payload.get("description", ""),
            "data_provider": payload.get("data_provider"),
            "data_provider_effective": payload.get("data_provider") or "auto",
            "status": "configured",
            "cycles": None,
            "last_error": "",
            "watch_symbols": payload.get("watch_symbols") or [],
            "execution_strategy": payload.get("execution_strategy", ""),
            "account_id": payload.get("account_id", ""),
            "model_id": payload.get("model_id", ""),
            "settings": payload.get("settings"),
            "created_at": "2026-04-04T00:00:00",
            "updated_at": "2026-04-04T00:00:00",
        }
        self.instances[instance_id] = record
        return SimpleNamespace(
            instance_id=instance_id,
            config=SimpleNamespace(
                template_id=payload["template_id"],
                mode=mode,
            ),
        )

    async def get_instance_status(self, identifier):
        return self.instances[identifier]


class _FakeApprovalResult:
    def __init__(self, status: str, intent_id: str, approval_id: str):
        self.status = status
        self.intent_id = intent_id
        self.approval_id = approval_id


class _FakePendingApproval:
    def __init__(self):
        self.approval_id = "approval-1"
        self.intent_id = "intent-1"

        class _Timestamp:
            def isoformat(self):
                return "2026-04-04T00:00:00"

        self.created_at = _Timestamp()
        self.expires_at = _Timestamp()


class _FakeApprovalGate:
    def __init__(self):
        self.expire_calls = 0
        self.approve_calls = []
        self.reject_calls = []

    async def expire_pending(self):
        self.expire_calls += 1
        return ["expired-1"]

    async def list_pending(self):
        return [_FakePendingApproval()]

    async def approve(self, approval_id):
        self.approve_calls.append(approval_id)
        return _FakeApprovalResult(status="approved", intent_id="intent-1", approval_id=approval_id)

    async def reject(self, approval_id, reason=""):
        self.reject_calls.append((approval_id, reason))
        return _FakeApprovalResult(status="rejected", intent_id="intent-1", approval_id=approval_id)


class ApiAppTests(unittest.TestCase):
    def tearDown(self):
        reset_observability()

    def test_tick_endpoint_awaits_async_service_and_gate(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream, app=app)

        with TestClient(app) as client:
            response = client.post("/system/tick")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"executed": 2, "expired_count": 1})
        self.assertEqual(service.tick_calls, 1)
        self.assertEqual(approval_gate.expire_calls, 1)

    def test_state_and_approval_endpoints_await_async_dependencies(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            state_response = client.get("/system/state")
            kill_response = client.post("/system/kill-switch", json={"enabled": True})
            pending_response = client.get("/approvals/pending")
            approve_response = client.post("/approvals/approval-1/approve")
            reject_response = client.post("/approvals/approval-1/reject")

        self.assertEqual(state_response.status_code, 200)
        self.assertEqual(
            state_response.json(),
            {"kill_switch_enabled": False, "instance_count": 1, "running_count": 1},
        )
        self.assertEqual(kill_response.status_code, 200)
        self.assertEqual(service.kill_switch_calls, [True])
        self.assertEqual(pending_response.status_code, 200)
        self.assertEqual(len(pending_response.json()), 1)
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()["status"], "approved")
        self.assertEqual(reject_response.status_code, 200)
        self.assertEqual(reject_response.json()["status"], "rejected")
        self.assertEqual(approval_gate.approve_calls, ["approval-1"])
        self.assertEqual(approval_gate.reject_calls, [("approval-1", "api reject")])

    def test_create_instance_normalizes_business_fields(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/instances",
                json={
                    "name": "alpha-growth",
                    "template_id": "single-agent-trend",
                    "mode": "paper",
                    "orchestrator_mode": "single-agent",
                    "description": "demo",
                    "data_provider": " mock ",
                    "watch_symbols": [" AAPL ", "", "MSFT "],
                    "execution_strategy": "langchain",
                    "account_id": "acct-1",
                    "model_id": "gpt-4",
                    "settings": {"risk": "low"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(service.create_calls), 1)
        self.assertEqual(service.create_calls[0]["data_provider"], "mock")
        self.assertEqual(service.create_calls[0]["watch_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(
            response.json(),
            {
                "instance_id": "instance-1",
                "name": "alpha-growth",
                "template_id": "single-agent-trend",
                "mode": "paper",
                "orchestrator_mode": "single-agent",
                "description": "demo",
                "data_provider": "mock",
                "data_provider_effective": "mock",
                "status": "configured",
                "cycles": None,
                "last_error": "",
                "watch_symbols": ["AAPL", "MSFT"],
                "execution_strategy": "langchain",
                "account_id": "acct-1",
                "model_id": "gpt-4",
                "settings": {"risk": "low"},
                "created_at": "2026-04-04T00:00:00",
                "updated_at": "2026-04-04T00:00:00",
            },
        )

    def test_create_instance_rejects_non_object_settings(self):
        service = _FakeService()
        approval_gate = _FakeApprovalGate()
        app = create_app(service, approval_gate)

        with TestClient(app) as client:
            response = client.post(
                "/instances",
                json={
                    "name": "alpha-growth",
                    "template_id": "single-agent-trend",
                    "settings": ["not", "an", "object"],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("settings", response.text)
        self.assertEqual(service.create_calls, [])

    def test_build_api_with_runtime_uses_runtime_aclose_on_shutdown(self):
        closed = []

        class _ServerFakeService(_FakeService):
            async def aclose(self):
                closed.append("service")

        runtime = {
            "service": _ServerFakeService(),
            "approval_gate": _FakeApprovalGate(),
            "aclose": self._make_async_callback(closed, "runtime"),
        }
        fake_cfg = SimpleNamespace(
            observability=SimpleNamespace(
                service_name="tradeclaw-test",
                log_level="INFO",
                tracing_enabled=False,
                console_enabled=False,
            ),
            server=SimpleNamespace(tick_seconds=0.01),
        )

        with (
            patch("tradeclaw.api.server.get_config", return_value=fake_cfg),
            patch("tradeclaw.api.server.build_platform_runtime", new=self._make_runtime_builder(runtime)),
            patch("tradeclaw.api.server.initialize_observability"),
        ):
            app = asyncio.run(build_api_with_runtime())
            with TestClient(app):
                pass

        self.assertEqual(closed, ["runtime"])

    @staticmethod
    def _make_runtime_builder(runtime):
        async def _builder(*args, **kwargs):
            return runtime

        return _builder

    @staticmethod
    def _make_async_callback(calls, label):
        async def _callback():
            calls.append(label)

        return _callback


if __name__ == "__main__":
    unittest.main()
