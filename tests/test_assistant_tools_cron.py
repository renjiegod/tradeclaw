"""Unit tests for cron-related assistant tools.

Covers:

* Contract enforcement (``additionalProperties: false`` → ``unknown_arguments``
  for typos / drift).
* JSON-string coercion for ``pre_action``.
* Patch semantics for ``UpdateCronJobTool`` (only explicitly-provided fields
  are forwarded; ``pre_action=None`` is an explicit clear).
* The two new run-history tools: ``ListCronJobRunsTool`` and
  ``GetCronJobRunTool``.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.api.operations.cron_tools import (
    CreateCronJobTool,
    DeleteCronJobTool,
    GetCronJobRunTool,
    GetCronJobTool,
    ListCronJobRunsTool,
    ListCronJobsTool,
    PauseCronJobTool,
    ResumeCronJobTool,
    TriggerCronJobTool,
    UpdateCronJobTool,
)


class _FakeMgr:
    """Stand-in for ``AgentCronManager`` capturing call payloads."""

    def __init__(self) -> None:
        self.last_create_call: dict[str, Any] | None = None
        self.last_update_call: dict[str, Any] | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.create_should_raise: ValueError | None = None
        self.update_should_raise: ValueError | None = None

    async def create_job(self, data: dict[str, Any]) -> dict[str, Any]:
        self.last_create_call = dict(data)
        if self.create_should_raise is not None:
            raise self.create_should_raise
        job = {"id": "cron-1", **data}
        self.jobs[job["id"]] = job
        return job

    async def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.last_update_call = {"job_id": job_id, "updates": dict(updates)}
        if self.update_should_raise is not None:
            raise self.update_should_raise
        existing = self.jobs.get(job_id, {"id": job_id, "enabled": True})
        merged = {**existing, **updates, "id": job_id}
        self.jobs[job_id] = merged
        return merged

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        return [j for j in self.jobs.values() if j.get("agent_id") == agent_id]

    async def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)

    async def pause_job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "enabled": False}

    async def resume_job(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "enabled": True}

    async def trigger_job(self, job_id: str) -> str:
        return f"run-{job_id}"


class _FakeRunRepo:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self._items = list(items or [])
        self.last_list_call: dict[str, Any] | None = None
        self.last_get_call: str | None = None

    async def list_for_job(self, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        self.last_list_call = {"job_id": job_id, "limit": limit}
        return [it for it in self._items if it.get("job_id") == job_id][:limit]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.last_get_call = run_id
        for it in self._items:
            if it.get("id") == run_id:
                return it
        return None


_CRON_MIN_CREATE_PAYLOAD = {
    "agent_id": "a1",
    "name": "n",
    "schedule": {"kind": "cron", "expr": "* * * * *"},
    "input_template": "x",
}


class CreateCronJobToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_kwarg(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(**_CRON_MIN_CREATE_PAYLOAD, bogus="boom")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)
        self.assertIn("bogus", res.text)

    async def test_accepts_minimum_payload_without_pre_action(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(**_CRON_MIN_CREATE_PAYLOAD)
        self.assertFalse(res.is_error)
        self.assertIn("cron-1", res.text)
        self.assertIn("Created cron job", res.text)
        # pre_action is forwarded as None when not supplied.
        self.assertIsNone(mgr.last_create_call["pre_action"])

    async def test_accepts_pre_action_dict(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **_CRON_MIN_CREATE_PAYLOAD,
            pre_action={"kind": "noop", "params": {}},
        )
        self.assertFalse(res.is_error)
        self.assertIn("Created cron job", res.text)
        self.assertEqual(
            mgr.last_create_call["pre_action"], {"kind": "noop", "params": {}}
        )

    async def test_coerces_pre_action_json_string(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **_CRON_MIN_CREATE_PAYLOAD,
            pre_action='{"kind": "noop", "params": {}}',
        )
        self.assertFalse(res.is_error)
        self.assertIn("Created cron job", res.text)
        self.assertEqual(
            mgr.last_create_call["pre_action"], {"kind": "noop", "params": {}}
        )

    async def test_rejects_pre_action_missing_kind(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **_CRON_MIN_CREATE_PAYLOAD,
            pre_action={"params": {}},
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:invalid_pre_action]", res.text)

    async def test_rejects_pre_action_non_object(self) -> None:
        # A non-string scalar is not coerced (it's not a JSON string), and it's
        # not a valid native dict — coercion rejects with invalid_pre_action_json.
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **_CRON_MIN_CREATE_PAYLOAD,
            pre_action=42,
        )
        self.assertTrue(res.is_error)
        self.assertTrue(
            "[error:invalid_pre_action_json]" in res.text
            or "[error:invalid_pre_action]" in res.text
        )

    async def test_validation_error_from_manager_returns_structured_error(self) -> None:
        mgr = _FakeMgr()
        mgr.create_should_raise = ValueError("bad cron expression")
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(**_CRON_MIN_CREATE_PAYLOAD)
        self.assertTrue(res.is_error)
        self.assertIn("[error:validation_error]", res.text)
        self.assertIn("bad cron", res.text)

    async def test_missing_required_field(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        # Drop agent_id.
        partial = {k: v for k, v in _CRON_MIN_CREATE_PAYLOAD.items() if k != "agent_id"}
        res = await tool.execute(**partial)
        self.assertTrue(res.is_error)
        self.assertIn("[error:missing_required]", res.text)
        self.assertIn("agent_id", res.text)

    async def test_opts_into_calling_agent_id_injection(self) -> None:
        """The schema marks agent_id optional and opts the tool into the
        registry-level calling-agent-id injection. Without that opt-in the
        model would be forced to type its own id (and historically tried to
        invent placeholder strings like "当前agent")."""
        tool = CreateCronJobTool(_FakeMgr())
        self.assertTrue(tool.requires_calling_agent_id)
        self.assertNotIn("agent_id", tool.parameters["required"])
        # Schema property still present so the registry can inject under it.
        self.assertIn("agent_id", tool.parameters["properties"])

    async def test_registry_injects_calling_agent_id_when_omitted(self) -> None:
        from doyoutrade.tools import OperationRegistry

        mgr = _FakeMgr()
        registry = OperationRegistry([CreateCronJobTool(mgr)])
        args = {k: v for k, v in _CRON_MIN_CREATE_PAYLOAD.items() if k != "agent_id"}
        result = await registry.execute(
            "create_cron_job",
            args,
            session_id="s1",
            calling_agent_id="agent-caller",
        )
        self.assertFalse(getattr(result, "is_error", False))
        self.assertEqual(mgr.last_create_call["agent_id"], "agent-caller")

    async def test_registry_respects_explicit_agent_id(self) -> None:
        from doyoutrade.tools import OperationRegistry

        mgr = _FakeMgr()
        registry = OperationRegistry([CreateCronJobTool(mgr)])
        await registry.execute(
            "create_cron_job",
            {**_CRON_MIN_CREATE_PAYLOAD, "agent_id": "agent-explicit"},
            session_id="s1",
            calling_agent_id="agent-caller",
        )
        # Explicit value must win — supports scheduling for another agent.
        self.assertEqual(mgr.last_create_call["agent_id"], "agent-explicit")

    async def test_timezone_is_fixed_to_asia_shanghai(self) -> None:
        """Tool no longer exposes timezone; jobs are always created in
        Asia/Shanghai (UTC+8). The schema must reject timezone= as drift
        instead of silently letting the model pick something else."""
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        await tool.execute(**_CRON_MIN_CREATE_PAYLOAD)
        self.assertEqual(mgr.last_create_call["timezone"], "Asia/Shanghai")

        res = await tool.execute(**_CRON_MIN_CREATE_PAYLOAD, timezone="UTC")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_rejects_trimmed_kwargs(self) -> None:
        """max_concurrency / timeout_seconds / enabled have been removed from
        the create_cron_job tool schema. Manager defaults still apply, and
        pause/resume cover the enable/disable workflow."""
        tool = CreateCronJobTool(_FakeMgr())
        for stray in ("max_concurrency", "timeout_seconds", "enabled"):
            res = await tool.execute(**_CRON_MIN_CREATE_PAYLOAD, **{stray: 1})
            self.assertTrue(res.is_error, f"{stray} should be rejected")
            self.assertIn("[error:unknown_arguments]", res.text)
            self.assertIn(stray, res.text)

    async def test_rejects_legacy_cron_expression_top_level(self) -> None:
        """The legacy raw ``cron_expression`` field has been replaced by the
        tagged-union ``schedule``. Supplying it at the top level must surface
        as ``unknown_arguments`` so the model retries with ``schedule``."""
        tool = CreateCronJobTool(_FakeMgr())
        payload = {
            "agent_id": "a1",
            "name": "n",
            "cron_expression": "* * * * *",
            "input_template": "x",
        }
        res = await tool.execute(**payload)
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)
        self.assertIn("cron_expression", res.text)


class CreateCronJobScheduleKindTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end coverage of the new ``schedule`` tagged-union field.

    These tests exercise the path that the ``tmp/error_request.json``
    regression went through: the model picks a ``schedule.kind`` and
    field, the server (not the model) resolves the cron expression. They
    are intentionally executed via ``CreateCronJobTool.execute(...)``
    instead of probing ``_cron_schedule.normalize_schedule`` directly so
    the wiring (coercion / contract / debug events / manager payload) is
    covered.
    """

    @staticmethod
    def _base(schedule: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent_id": "a1",
            "name": "n",
            "schedule": schedule,
            "input_template": "x",
        }

    async def test_schedule_required(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        payload = {k: v for k, v in _CRON_MIN_CREATE_PAYLOAD.items() if k != "schedule"}
        res = await tool.execute(**payload)
        self.assertTrue(res.is_error)
        self.assertIn("[error:missing_required]", res.text)
        self.assertIn("schedule", res.text)

    async def test_schedule_cron_passthrough(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **self._base({"kind": "cron", "expr": "0 9 * * 1-5"})
        )
        self.assertFalse(res.is_error)
        self.assertEqual(mgr.last_create_call["cron_expression"], "0 9 * * 1-5")
        self.assertIn("kind=cron", res.text)

    async def test_schedule_once_at_with_delay_seconds(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        # 120s in the future from the wall-clock; we don't pin the clock so we
        # only assert structural shape — the minute/hour fields are integers
        # and the expression is 5 fields with day-of-week '*'.
        res = await tool.execute(
            **self._base({"kind": "once_at", "delay_seconds": 120})
        )
        self.assertFalse(res.is_error)
        expr = mgr.last_create_call["cron_expression"]
        parts = expr.split()
        self.assertEqual(len(parts), 5, f"expected 5-field cron, got {expr!r}")
        self.assertEqual(parts[4], "*")
        for component in parts[:4]:
            int(component)  # raises if not an integer

    async def test_schedule_once_at_sub_minute_rounds_up_with_note(self) -> None:
        """The sub-minute regression: user says '30秒后', cron has 1-min
        resolution, so server rounds up to the next whole minute and
        surfaces a note so the agent can truthfully report the actual
        fire time."""
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **self._base({"kind": "once_at", "delay_seconds": 30})
        )
        self.assertFalse(res.is_error)
        self.assertIn("Note:", res.text)
        self.assertIn("rounded up", res.text)
        # Resulting cron must still be a valid 5-field expression.
        expr = mgr.last_create_call["cron_expression"]
        self.assertEqual(len(expr.split()), 5)

    async def test_schedule_once_at_past_rejects(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **self._base({"kind": "once_at", "at": "2000-01-01T00:00:00+08:00"})
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:once_at_in_past]", res.text)

    async def test_schedule_once_at_requires_at_or_delay(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(**self._base({"kind": "once_at"}))
        self.assertTrue(res.is_error)
        self.assertIn("[error:missing_once_at_target]", res.text)

    async def test_schedule_once_at_conflicting_target_rejected(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **self._base(
                {
                    "kind": "once_at",
                    "at": "2099-01-01T00:00:00+08:00",
                    "delay_seconds": 60,
                }
            )
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:conflicting_once_at_target]", res.text)

    async def test_schedule_every_5min(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **self._base({"kind": "every", "every_seconds": 300})
        )
        self.assertFalse(res.is_error)
        self.assertEqual(mgr.last_create_call["cron_expression"], "*/5 * * * *")
        self.assertIn("kind=every", res.text)

    async def test_schedule_every_2h(self) -> None:
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            **self._base({"kind": "every", "every_seconds": 7200})
        )
        self.assertFalse(res.is_error)
        self.assertEqual(mgr.last_create_call["cron_expression"], "0 */2 * * *")

    async def test_schedule_every_sub_minute_rejected(self) -> None:
        """Sub-minute intervals route through once_at, not every. The
        wrong path must return a structured error pointing the agent at
        the right tool shape."""
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **self._base({"kind": "every", "every_seconds": 30})
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:every_seconds_below_minimum]", res.text)
        # Repair hint must mention the canonical alternative.
        self.assertIn("once_at", res.text)

    async def test_schedule_every_uneven_minutes_rejected(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        # 7 minutes does not divide 60 evenly.
        res = await tool.execute(
            **self._base({"kind": "every", "every_seconds": 7 * 60})
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:every_seconds_uneven]", res.text)

    async def test_schedule_missing_kind_rejected(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(**self._base({"expr": "* * * * *"}))
        self.assertTrue(res.is_error)
        self.assertIn("[error:missing_schedule_kind]", res.text)

    async def test_schedule_invalid_kind_rejected(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(**self._base({"kind": "yearly", "expr": "* * * * *"}))
        self.assertTrue(res.is_error)
        self.assertIn("[error:invalid_schedule_kind]", res.text)

    async def test_schedule_coerced_from_json_string(self) -> None:
        """Weaker models occasionally serialize nested object kwargs as
        JSON strings. Same coercion path used by ``pre_action`` applies
        to ``schedule`` so the call doesn't fail opaquely."""
        mgr = _FakeMgr()
        tool = CreateCronJobTool(mgr)
        res = await tool.execute(
            agent_id="a1",
            name="n",
            schedule='{"kind": "cron", "expr": "0 9 * * 1-5"}',
            input_template="x",
        )
        self.assertFalse(res.is_error)
        self.assertEqual(mgr.last_create_call["cron_expression"], "0 9 * * 1-5")

    async def test_schedule_extra_field_rejected(self) -> None:
        tool = CreateCronJobTool(_FakeMgr())
        res = await tool.execute(
            **self._base(
                {"kind": "cron", "expr": "0 9 * * *", "every_seconds": 60}
            )
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:unexpected_schedule_fields]", res.text)


class UpdateCronJobToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_kwarg(self) -> None:
        tool = UpdateCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="boom")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)
        self.assertIn("bogus", res.text)

    async def test_patch_semantics_only_provided_fields(self) -> None:
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(job_id="c1", name="renamed")
        self.assertFalse(res.is_error)
        self.assertIn("Updated cron job c1", res.text)
        self.assertIsNotNone(mgr.last_update_call)
        self.assertEqual(
            set(mgr.last_update_call["updates"].keys()), {"name"}
        )
        self.assertEqual(mgr.last_update_call["updates"]["name"], "renamed")

    async def test_pre_action_null_clears(self) -> None:
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(job_id="c1", pre_action=None)
        self.assertFalse(res.is_error)
        self.assertIn("Updated cron job c1", res.text)
        # pre_action=None is an explicit clear, forwarded to the manager.
        self.assertEqual(mgr.last_update_call["updates"], {"pre_action": None})

    async def test_pre_action_object_forwarded(self) -> None:
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(
            job_id="c1",
            pre_action={"kind": "noop", "params": {"instance_id": "si-1"}},
        )
        self.assertFalse(res.is_error)
        self.assertIn("Updated cron job c1", res.text)
        self.assertEqual(
            mgr.last_update_call["updates"]["pre_action"],
            {"kind": "noop", "params": {"instance_id": "si-1"}},
        )

    async def test_pre_action_missing_kind_is_rejected(self) -> None:
        tool = UpdateCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", pre_action={"params": {}})
        self.assertTrue(res.is_error)
        self.assertIn("[error:invalid_pre_action]", res.text)

    async def test_pre_action_coerced_from_json_string(self) -> None:
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(
            job_id="c1", pre_action='{"kind": "noop", "params": {}}'
        )
        self.assertFalse(res.is_error)
        self.assertIn("Updated cron job c1", res.text)
        self.assertEqual(
            mgr.last_update_call["updates"]["pre_action"],
            {"kind": "noop", "params": {}},
        )

    async def test_manager_value_error_surfaces_structured(self) -> None:
        mgr = _FakeMgr()
        mgr.update_should_raise = ValueError("not found")
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(job_id="c1", name="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:validation_error]", res.text)

    async def test_schedule_update_translates_to_cron_expression(self) -> None:
        """Updating with a ``schedule`` object must surface as the legacy
        ``cron_expression`` key on the manager-update payload — the DB
        schema only knows about a flat cron string."""
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(
            job_id="c1",
            schedule={"kind": "every", "every_seconds": 300},
        )
        self.assertFalse(res.is_error)
        updates = mgr.last_update_call["updates"]
        self.assertEqual(updates.get("cron_expression"), "*/5 * * * *")
        # ``schedule`` itself must NOT leak into the update payload — the
        # manager wouldn't know what to do with it.
        self.assertNotIn("schedule", updates)

    async def test_schedule_update_rejects_invalid_kind(self) -> None:
        mgr = _FakeMgr()
        tool = UpdateCronJobTool(mgr)
        res = await tool.execute(
            job_id="c1",
            schedule={"kind": "every", "every_seconds": 30},
        )
        self.assertTrue(res.is_error)
        self.assertIn("[error:every_seconds_below_minimum]", res.text)
        # Failed normalization must not poke the manager.
        self.assertIsNone(mgr.last_update_call)

    async def test_legacy_cron_expression_top_level_rejected(self) -> None:
        tool = UpdateCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", cron_expression="* * * * *")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)
        self.assertIn("cron_expression", res.text)


class SecondaryCronToolContractTests(unittest.IsolatedAsyncioTestCase):
    """The six minor cron tools enforce additionalProperties: false."""

    async def test_list_cron_jobs_rejects_unknown_kwarg(self) -> None:
        tool = ListCronJobsTool(_FakeMgr())
        res = await tool.execute(agent_id="a1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_list_cron_jobs_defaults_to_calling_agent_via_registry(self) -> None:
        from doyoutrade.tools import OperationRegistry

        mgr = _FakeMgr()
        mgr.jobs["c1"] = {
            "id": "c1", "agent_id": "agent-caller", "name": "n",
            "cron_expression": "* * * * *", "enabled": True,
        }
        registry = OperationRegistry([ListCronJobsTool(mgr)])
        result = await registry.execute(
            "list_cron_jobs",
            {},
            session_id="s1",
            calling_agent_id="agent-caller",
        )
        # Returns a plain str on success — the calling agent's job appears.
        self.assertIn("c1", str(result))
        self.assertIn("agent-caller", str(result))

    async def test_get_cron_job_rejects_unknown_kwarg(self) -> None:
        tool = GetCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_delete_cron_job_rejects_unknown_kwarg(self) -> None:
        tool = DeleteCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_pause_cron_job_rejects_unknown_kwarg(self) -> None:
        tool = PauseCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_resume_cron_job_rejects_unknown_kwarg(self) -> None:
        tool = ResumeCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_trigger_cron_job_rejects_unknown_kwarg(self) -> None:
        tool = TriggerCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_get_cron_job_not_found(self) -> None:
        tool = GetCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="nope")
        self.assertTrue(res.is_error)
        self.assertIn("[error:not_found]", res.text)

    async def test_trigger_cron_job_happy_path(self) -> None:
        tool = TriggerCronJobTool(_FakeMgr())
        res = await tool.execute(job_id="c1")
        self.assertFalse(res.is_error)
        self.assertIn("Triggered cron job c1", res.text)
        self.assertIn("run-c1", res.text)


class CronRunHistoryToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_cron_job_runs_returns_items(self) -> None:
        items = [
            {"id": "r1", "job_id": "c1", "status": "completed"},
            {"id": "r2", "job_id": "c1", "status": "running"},
            {"id": "r3", "job_id": "c2", "status": "completed"},
        ]
        repo = _FakeRunRepo(items=items)
        tool = ListCronJobRunsTool(repo)
        res = await tool.execute(job_id="c1", limit=5)
        self.assertFalse(res.is_error)
        self.assertIn("Found 2 run(s) for cron job c1", res.text)
        self.assertIn("r1", res.text)
        self.assertIn("r2", res.text)
        self.assertEqual(repo.last_list_call, {"job_id": "c1", "limit": 5})

    async def test_list_cron_job_runs_default_limit(self) -> None:
        repo = _FakeRunRepo(items=[])
        tool = ListCronJobRunsTool(repo)
        await tool.execute(job_id="c1")
        self.assertEqual(repo.last_list_call, {"job_id": "c1", "limit": 20})

    async def test_list_cron_job_runs_clamps_limit(self) -> None:
        repo = _FakeRunRepo(items=[])
        tool = ListCronJobRunsTool(repo)
        await tool.execute(job_id="c1", limit=10_000)
        self.assertEqual(repo.last_list_call["limit"], 200)
        await tool.execute(job_id="c1", limit=0)
        self.assertEqual(repo.last_list_call["limit"], 1)

    async def test_list_cron_job_runs_rejects_unknown_kwarg(self) -> None:
        tool = ListCronJobRunsTool(_FakeRunRepo())
        res = await tool.execute(job_id="c1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)

    async def test_get_cron_job_run_happy(self) -> None:
        repo = _FakeRunRepo(items=[{"id": "r1", "job_id": "c1"}])
        tool = GetCronJobRunTool(repo)
        res = await tool.execute(run_id="r1")
        self.assertFalse(res.is_error)
        self.assertIn("Cron job run r1", res.text)
        # The JSON payload embeds the full row so the model can parse it.
        self.assertIn('"id": "r1"', res.text)
        self.assertIn('"status": "ok"', res.text)

    async def test_get_cron_job_run_not_found(self) -> None:
        repo = _FakeRunRepo(items=[])
        tool = GetCronJobRunTool(repo)
        res = await tool.execute(run_id="nope")
        self.assertTrue(res.is_error)
        self.assertIn("[error:not_found]", res.text)

    async def test_get_cron_job_run_rejects_unknown_kwarg(self) -> None:
        tool = GetCronJobRunTool(_FakeRunRepo())
        res = await tool.execute(run_id="r1", bogus="x")
        self.assertTrue(res.is_error)
        self.assertIn("[error:unknown_arguments]", res.text)


class SecondaryCronToolDebugEventTests(unittest.IsolatedAsyncioTestCase):
    """The 8 secondary cron tools must emit .request + terminal-verb events on success."""

    async def test_list_cron_jobs_emits_request_and_ok_events(self) -> None:
        mgr = _FakeMgr()
        mgr.jobs["c1"] = {"id": "c1", "agent_id": "a1"}
        tool = ListCronJobsTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(agent_id="a1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_list_cron_jobs.request", event_names)
        self.assertIn("operation_list_cron_jobs.ok", event_names)
        ok_payload = next(
            c.args[1] for c in emit.await_args_list
            if c.args[0] == "operation_list_cron_jobs.ok"
        )
        self.assertEqual(ok_payload["count"], 1)

    async def test_get_cron_job_emits_request_and_ok_events(self) -> None:
        mgr = _FakeMgr()
        mgr.jobs["c1"] = {"id": "c1", "agent_id": "a1"}
        tool = GetCronJobTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_get_cron_job.request", event_names)
        self.assertIn("operation_get_cron_job.ok", event_names)

    async def test_delete_cron_job_emits_request_and_deleted_events(self) -> None:
        mgr = _FakeMgr()
        tool = DeleteCronJobTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_delete_cron_job.request", event_names)
        self.assertIn("operation_delete_cron_job.deleted", event_names)

    async def test_pause_cron_job_emits_request_and_paused_events(self) -> None:
        mgr = _FakeMgr()
        tool = PauseCronJobTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_pause_cron_job.request", event_names)
        self.assertIn("operation_pause_cron_job.paused", event_names)

    async def test_resume_cron_job_emits_request_and_resumed_events(self) -> None:
        mgr = _FakeMgr()
        tool = ResumeCronJobTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_resume_cron_job.request", event_names)
        self.assertIn("operation_resume_cron_job.resumed", event_names)

    async def test_trigger_cron_job_emits_request_and_triggered_events(self) -> None:
        mgr = _FakeMgr()
        tool = TriggerCronJobTool(mgr)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_trigger_cron_job.request", event_names)
        self.assertIn("operation_trigger_cron_job.triggered", event_names)
        triggered_payload = next(
            c.args[1] for c in emit.await_args_list
            if c.args[0] == "operation_trigger_cron_job.triggered"
        )
        self.assertEqual(triggered_payload["cron_job_run_id"], "run-c1")

    async def test_list_cron_job_runs_emits_request_and_ok_events(self) -> None:
        repo = _FakeRunRepo(items=[{"id": "r1", "job_id": "c1"}])
        tool = ListCronJobRunsTool(repo)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(job_id="c1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_list_cron_job_runs.request", event_names)
        self.assertIn("operation_list_cron_job_runs.ok", event_names)
        ok_payload = next(
            c.args[1] for c in emit.await_args_list
            if c.args[0] == "operation_list_cron_job_runs.ok"
        )
        self.assertEqual(ok_payload["count"], 1)

    async def test_get_cron_job_run_emits_request_and_ok_events(self) -> None:
        repo = _FakeRunRepo(items=[{"id": "r1", "job_id": "c1"}])
        tool = GetCronJobRunTool(repo)
        with patch(
            "doyoutrade.api.operations.cron_tools.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            await tool.execute(run_id="r1")
        event_names = [c.args[0] for c in emit.await_args_list]
        self.assertIn("operation_get_cron_job_run.request", event_names)
        self.assertIn("operation_get_cron_job_run.ok", event_names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
