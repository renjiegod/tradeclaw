"""API contract tests for the monitor (盯盘) routes.

Uses a fake service holding in-memory monitor repos (the same pattern as the
other ``test_api_app`` route tests) so the FastAPI TestClient drives the real
``/monitors`` handlers — validation, error_codes, patch semantics, run-once
placeholders — without a live DB.
"""

from __future__ import annotations

import unittest
import uuid
from dataclasses import dataclass

from fastapi.testclient import TestClient

from doyoutrade.api.app import create_app
from doyoutrade.persistence.errors import RecordNotFoundError


@dataclass
class _Rule:
    id: str
    name: str
    enabled: bool
    status: str
    scope_kind: str
    scope_json: dict
    condition_json: dict
    delivery_json: dict | None
    cooldown_seconds: int
    last_error: str = ""
    created_at: str = "2026-06-22T00:00:00"
    updated_at: str = "2026-06-22T00:00:00"


class _MemRuleRepo:
    def __init__(self):
        self.d: dict[str, _Rule] = {}

    async def create_rule(self, **kw):
        rid = kw.pop("id", None) or f"mon-{uuid.uuid4().hex[:12]}"
        r = _Rule(id=rid, **kw)
        self.d[rid] = r
        return r

    async def get_rule(self, rid):
        if rid not in self.d:
            raise RecordNotFoundError(f"monitor rule not found: {rid}")
        return self.d[rid]

    async def list_rules(self):
        return list(self.d.values())

    async def list_active(self):
        return [r for r in self.d.values() if r.enabled and r.status == "active"]

    async def update_rule(self, rid, **fields):
        r = self.d[rid]
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    async def delete_rule(self, rid):
        self.d.pop(rid, None)


class _MemAlertRepo:
    async def list_for_rule(self, rid, *, symbol=None, limit=100):
        return []


class _MemWatch:
    async def list_entries(self, tag=None):
        return [{"symbol": "000001.SZ", "display_name": "平安银行"}]


class _FakeService:
    def __init__(self):
        self.monitor_rule_repository = _MemRuleRepo()
        self.monitor_alert_repository = _MemAlertRepo()
        self.watchlist_repository = _MemWatch()
        self.cycle_run_repository = None


class _FakeGate:
    pass


def _client():
    return TestClient(create_app(_FakeService(), _FakeGate(), quote_stream_service=None))


class MonitorApiTests(unittest.TestCase):
    def test_create_get_list_delete(self):
        c = _client()
        r = c.post(
            "/monitors",
            json={
                "name": "半导体涨停",
                "scope_kind": "watchlist_tag",
                "scope": {"tag": "半导体"},
                "condition_json": {"preset": "limit_up"},
                "channel_id": "chan-x",
                "chat_id": "oc_1",
            },
        )
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertTrue(body["id"].startswith("mon-"))
        self.assertEqual(body["cooldown_seconds"], 300)  # default
        self.assertEqual(body["delivery_json"]["target"]["kind"], "channel")
        mid = body["id"]

        self.assertEqual(c.get("/monitors").json()["total"], 1)
        self.assertEqual(c.get(f"/monitors/{mid}").status_code, 200)
        self.assertEqual(c.delete(f"/monitors/{mid}").json()["status"], "deleted")
        self.assertEqual(c.get("/monitors").json()["total"], 0)

    def test_get_404_error_code(self):
        c = _client()
        resp = c.get("/monitors/mon-nope")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["error_code"], "monitor_not_found")

    def test_bad_condition_400(self):
        c = _client()
        resp = c.post(
            "/monitors",
            json={"name": "x", "scope_kind": "symbols", "scope": {"symbols": ["000001.SZ"]}, "condition_json": {"preset": "nope"}},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "condition_preset_unknown")

    def test_empty_scope_400(self):
        c = _client()
        resp = c.post(
            "/monitors",
            json={"name": "x", "scope_kind": "symbols", "scope": {"symbols": []}, "condition_json": {"preset": "limit_up"}},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "monitor_scope_empty")

    def test_update_patch_semantics(self):
        c = _client()
        mid = c.post(
            "/monitors",
            json={"name": "x", "scope_kind": "symbols", "scope": {"symbols": ["000001.SZ"]}, "condition_json": {"preset": "limit_up"}},
        ).json()["id"]
        upd = c.put(f"/monitors/{mid}", json={"enabled": False, "cooldown_seconds": 600})
        self.assertEqual(upd.status_code, 200)
        self.assertFalse(upd.json()["enabled"])
        self.assertEqual(upd.json()["cooldown_seconds"], 600)
        # condition unchanged (patch) — stored as the validator-normalized tree
        self.assertEqual(upd.json()["condition_json"], {"preset": "limit_up", "params": {}})

    def test_run_once_placeholder_no_500(self):
        c = _client()
        mid = c.post(
            "/monitors",
            json={"name": "x", "scope_kind": "watchlist_tag", "scope": {"tag": "半导体"}, "condition_json": {"preset": "limit_up"}},
        ).json()["id"]
        resp = c.post(f"/monitors/{mid}/run-once")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["matched_count"], 0)
        self.assertEqual(data["symbols"][0]["status"], "qmt_disconnected")

    def test_alerts_404_for_missing_rule(self):
        c = _client()
        resp = c.get("/monitors/mon-nope/alerts")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["error_code"], "monitor_not_found")


if __name__ == "__main__":
    unittest.main()
