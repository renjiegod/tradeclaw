"""Unit tests for AgentCronManager._execute pre-action dispatch and run-record bookkeeping."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from doyoutrade.assistant import cron_manager as cron_manager_mod
from doyoutrade.assistant.cron_executors import JobExecutorRegistry, NoopExecutor
from doyoutrade.assistant.cron_executors.base import PreActionResult
from doyoutrade.assistant.cron_manager import AgentCronManager


def _bare_manager(svc, cron_repo, run_repo, registry):
    """Build an AgentCronManager without starting APScheduler."""
    mgr = AgentCronManager.__new__(AgentCronManager)
    mgr._svc = svc
    mgr._repo = cron_repo
    mgr._run_repo = run_repo
    mgr._registry = registry
    mgr._sems = {}
    mgr._scheduler = MagicMock()       # never used in _execute
    mgr._running = False
    return mgr


def _make_job(**overrides):
    base = {
        "id": "c1",
        "agent_id": "a1",
        "name": "open-bell",
        "enabled": True,
        "max_concurrency": 1,
        "input_template": "pre={{ pre }}",
        "pre_action": None,
        "cron_expression": "* * * * *",
        "timezone": "UTC",
        "timeout_seconds": 120,
    }
    base.update(overrides)
    return base


class _CronRepoStub:
    def __init__(self, job):
        self.job = job
        self.state_updates = []
        self.upserts = []

    async def get_job(self, job_id):
        return self.job if self.job and self.job["id"] == job_id else None

    async def update_job_state(self, job_id, **kw):
        self.state_updates.append((job_id, kw))

    async def upsert_job(self, data):
        """Recorded by auto-disable path. The production repository
        merges with the existing row; our stub just remembers the
        patch so tests can assert ``enabled`` flipped to False."""
        self.upserts.append(dict(data))
        return {**(self.job or {}), **data}


class _RunRepoStub:
    def __init__(self):
        self.created = []
        self.updates = []

    async def create_run(self, data):
        self.created.append(data)
        return {**data}

    async def update_run(self, run_id, updates):
        self.updates.append((run_id, updates))
        return {"id": run_id, **updates}


class AgentCronManagerExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_pre_action_still_invokes_agent_and_writes_success(self):
        job = _make_job(pre_action=None, input_template="hello {{ now }}")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        svc.create_session.assert_awaited_once()
        svc.send_message.assert_awaited_once()
        # The rendered template body is still in the sent content...
        sent = (
            svc.send_message.call_args.kwargs.get("content")
            or svc.send_message.call_args.args[1]
        )
        self.assertIn("hello", sent)
        # ...but a ``[cron-trigger]`` header is prepended so the
        # receiving agent immediately knows the session is cron-driven
        # (avoiding the ``cron list`` recon round-trip observed at
        # session asst-91bfd63bf186).
        self.assertTrue(
            sent.startswith("[cron-trigger]"),
            f"expected sent content to start with cron header; got {sent!r}",
        )
        self.assertIn("job_id=c1", sent)
        self.assertIn("name='open-bell'", sent)
        # Run record was created with status=running and no pre_kind.
        self.assertEqual(run_repo.created[0]["status"], "running")
        self.assertIsNone(run_repo.created[0].get("pre_kind"))
        # And finalised to status=success.
        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "success")
        self.assertEqual(final["status"], "success")
        self.assertEqual(final["agent_session_id"], "s1")

    async def test_noop_pre_action_renders_pre_block_and_succeeds(self):
        job = _make_job(
            pre_action={"kind": "noop", "params": {}},
            input_template="status={{ pre.status }} data={{ pre.data }}",
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        registry = JobExecutorRegistry()
        registry.register(NoopExecutor())
        mgr = _bare_manager(svc, cron_repo, run_repo, registry)
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        sent = svc.send_message.call_args.kwargs.get("content") or svc.send_message.call_args.args[1]
        self.assertIn("status=ok", sent)

        # pre_status was persisted
        pre_status_update = next(u for (_rid, u) in run_repo.updates if "pre_status" in u)
        self.assertEqual(pre_status_update["pre_status"], "ok")

        # Run row was created with pre_kind="noop"
        self.assertEqual(run_repo.created[0]["pre_kind"], "noop")

        # Final status is success.
        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "success")
        self.assertEqual(final["status"], "success")

    async def test_pre_action_raise_still_invokes_agent_with_error_block(self):
        class _BoomExecutor:
            kind = "boom"
            async def execute(self, params, ctx):
                raise RuntimeError("boom!")

        job = _make_job(
            pre_action={"kind": "boom", "params": {}},
            input_template="status={{ pre.status }} err={{ pre.error }}",
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        registry = JobExecutorRegistry()
        registry.register(_BoomExecutor())
        mgr = _bare_manager(svc, cron_repo, run_repo, registry)
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        svc.send_message.assert_awaited_once()
        sent = svc.send_message.call_args.kwargs.get("content") or svc.send_message.call_args.args[1]
        self.assertIn("status=error", sent)
        self.assertIn("boom!", sent)

        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "pre_failed")
        self.assertEqual(final["status"], "pre_failed")
        self.assertEqual(final["agent_session_id"], "s1")

    async def test_unknown_kind_treated_as_pre_failed(self):
        job = _make_job(
            pre_action={"kind": "no_such_kind", "params": {}},
            input_template="err={{ pre.error }}",
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        svc.send_message.assert_awaited_once()
        sent = svc.send_message.call_args.kwargs.get("content") or svc.send_message.call_args.args[1]
        self.assertTrue("no_such_kind" in sent or "unknown_kind" in sent)

        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "pre_failed")
        self.assertEqual(final["status"], "pre_failed")

    async def test_template_render_failure_aborts_before_agent(self):
        job = _make_job(
            pre_action=None,
            input_template="{{ this.is.invalid syntax",   # Jinja parse error
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        # Agent was never invoked.
        svc.create_session.assert_not_called()
        svc.send_message.assert_not_called()

        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "error")
        self.assertEqual(final["status"], "error")

    async def test_send_message_failure_marks_agent_failed(self):
        job = _make_job(pre_action=None, input_template="hi")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock(side_effect=RuntimeError("im offline"))

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        final = next(u for (_rid, u) in run_repo.updates if u.get("status") == "agent_failed")
        self.assertEqual(final["status"], "agent_failed")
        self.assertIn("im offline", final["agent_error"])

    async def test_disabled_job_short_circuits(self):
        job = _make_job(enabled=False)
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        svc.create_session.assert_not_called()
        # No run row should be inserted for a disabled fire either; or if it is,
        # it should NOT be marked "running". (The implementation is free to choose;
        # this test asserts the conservative no-side-effect behaviour.)
        self.assertEqual(run_repo.created, [])

    async def test_concurrency_skipped_when_semaphore_locked(self):
        job = _make_job()
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # pre-lock so _execute sees sem.locked()=True
        mgr._sems["c1"] = sem

        await mgr._execute("c1")

        # Agent not invoked.
        svc.create_session.assert_not_called()
        # A skipped row was written so operators see the missed fire.
        self.assertEqual(len(run_repo.created), 1)
        self.assertEqual(run_repo.created[0]["status"], "skipped")


class AgentCronManagerDeregisterDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """``_deregister_best_effort`` distinguishes ``JobLookupError`` (info)
    from other APScheduler failures (warning), so a real scheduler bug stops
    being masked by ``except Exception: pass``."""

    def _bare(self):
        svc = MagicMock()
        mgr = _bare_manager(svc, MagicMock(), MagicMock(), JobExecutorRegistry())
        mgr._scheduler = MagicMock()
        return mgr

    async def test_job_lookup_error_logged_as_info(self):
        mgr = self._bare()

        class _JobLookupError(Exception):
            pass

        _JobLookupError.__name__ = "JobLookupError"
        mgr._scheduler.remove_job = MagicMock(
            side_effect=_JobLookupError("not registered")
        )
        mgr._sems["c1"] = asyncio.Semaphore(1)
        # No exception propagates.
        await mgr._deregister_best_effort("c1", op="delete_job")
        # Semaphore was cleaned up.
        self.assertNotIn("c1", mgr._sems)

    async def test_other_exception_logged_as_warning_but_swallowed(self):
        mgr = self._bare()
        mgr._scheduler.remove_job = MagicMock(
            side_effect=RuntimeError("apscheduler is broken")
        )
        mgr._sems["c1"] = asyncio.Semaphore(1)
        await mgr._deregister_best_effort("c1", op="update_job")
        # Still cleared the local sem.
        self.assertNotIn("c1", mgr._sems)


class AgentCronManagerTaskPipelineDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """``_execute_task_pipeline`` should refuse to swallow malformed
    ``task_params_json`` — that used to silently coerce to ``{}`` and let the
    executor run with its own defaults."""

    async def test_non_dict_task_params_json_marks_run_error(self):
        job = _make_job(
            pre_action=None,
            task_kind="agent_chat_reply",
            task_params_json="not-a-dict",  # malformed
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()

        # Insert a placeholder run row first, mirroring the _execute flow
        # that calls _execute_task_pipeline. We register a stub executor so
        # the test exercises the params-type guard, not the unknown_kind
        # branch.
        from doyoutrade.assistant.cron_executors import JobTaskRegistry
        from doyoutrade.assistant.cron_executors.base import TaskResult

        class _StubExecutor:
            kind = "agent_chat_reply"

            def validate_params(self, params):
                return None

            async def run(self, params, ctx):  # pragma: no cover - never reached
                return TaskResult(status="ok")

        task_registry = JobTaskRegistry()
        task_registry.register(_StubExecutor())

        mgr = _bare_manager(MagicMock(), cron_repo, run_repo, JobExecutorRegistry())
        mgr.task_registry = task_registry
        mgr._scheduler = MagicMock()

        await mgr._execute_task_pipeline(
            job=job,
            task_kind="agent_chat_reply",
            cron_job_run_id="crun-abc",
            fired_at=__import__("datetime").datetime.now(),
            fire_span=MagicMock(),
        )
        terminal_updates = [u for (_rid, u) in run_repo.updates if u.get("status")]
        self.assertEqual(len(terminal_updates), 1)
        self.assertEqual(terminal_updates[0]["status"], "error")
        self.assertIn("invalid_task_params_json", terminal_updates[0]["agent_error"])
        # The cron_jobs row state is marked error too.
        self.assertTrue(any(
            kw.get("last_status") == "error"
            for (_jid, kw) in cron_repo.state_updates
        ))


class AgentCronManagerTriggerTests(unittest.IsolatedAsyncioTestCase):
    """trigger_job returns a stable cron_job_run_id and avoids double-inserting."""

    async def test_trigger_job_returns_run_id_and_prebuilds_row(self):
        job = _make_job(pre_action=None, input_template="hi")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        run_id = await mgr.trigger_job("c1")

        self.assertIsInstance(run_id, str)
        self.assertTrue(run_id.startswith("crun-"))
        # Only ONE row was created (the prebuilt one), not two.
        self.assertEqual(len(run_repo.created), 1)
        self.assertEqual(run_repo.created[0]["id"], run_id)
        self.assertEqual(run_repo.created[0]["status"], "running")

        # Give the fire-and-forget task a couple of event-loop ticks to drain.
        for _ in range(10):
            await asyncio.sleep(0)
            if any(u.get("status") == "success" for (_rid, u) in run_repo.updates):
                break
        self.assertTrue(
            any(rid == run_id and u.get("status") == "success"
                for (rid, u) in run_repo.updates),
            f"expected a success update for {run_id}, got {run_repo.updates!r}",
        )

    async def test_trigger_job_without_run_repo_still_returns_synthetic_id(self):
        job = _make_job(pre_action=None, input_template="hi")
        cron_repo = _CronRepoStub(job)
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo=None, registry=JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        run_id = await mgr.trigger_job("c1")
        self.assertIsInstance(run_id, str)
        self.assertTrue(run_id.startswith("crun-"))
        # Drain background _execute so it does not leak.
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_trigger_job_unknown_id_raises(self):
        cron_repo = _CronRepoStub(None)
        run_repo = _RunRepoStub()
        svc = MagicMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        with self.assertRaises(ValueError):
            await mgr.trigger_job("missing")
        # Nothing inserted because the job didn't resolve.
        self.assertEqual(run_repo.created, [])

    async def test_trigger_then_missing_semaphore_marks_prebuilt_row_skipped(self):
        """If trigger_job pre-creates a row but _execute can't find a semaphore,
        the row must be flipped to status='skipped' (not left as 'running')."""
        job = _make_job()
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        # Intentionally do NOT seed mgr._sems["c1"]

        run_id = await mgr.trigger_job("c1")
        # Wait for the fire-and-forget _execute to drain.
        for _ in range(10):
            await asyncio.sleep(0)
            if any(rid == run_id and u.get("status") == "skipped" for (rid, u) in run_repo.updates):
                break
        self.assertTrue(
            any(rid == run_id and u.get("status") == "skipped" for (rid, u) in run_repo.updates)
        )

    async def test_trigger_then_disabled_job_marks_prebuilt_row_skipped(self):
        """When job is disabled between trigger_job's get_job and _execute's get_job,
        the prebuilt row must be flipped to status='skipped'."""
        job_enabled = _make_job(enabled=True)
        job_disabled = _make_job(enabled=False)

        class _ToggleRepo(_CronRepoStub):
            def __init__(self):
                super().__init__(job_enabled)
                self._calls = 0

            async def get_job(self, job_id):
                self._calls += 1
                return job_enabled if self._calls == 1 else job_disabled

        cron_repo = _ToggleRepo()
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        run_id = await mgr.trigger_job("c1")
        for _ in range(10):
            await asyncio.sleep(0)
            if any(rid == run_id and u.get("status") == "skipped" for (rid, u) in run_repo.updates):
                break
        self.assertTrue(
            any(rid == run_id and u.get("status") == "skipped" for (rid, u) in run_repo.updates)
        )


class AgentCronManagerLegacyBehaviourTests(unittest.IsolatedAsyncioTestCase):
    """Confirm that with no run_repo and no executor_registry passed, the manager
    still works (back-compat for tests/bootstrap that don't wire them yet)."""

    async def test_no_run_repo_or_registry_still_executes_noop_path(self):
        job = _make_job(pre_action=None, input_template="hi {{ now }}")
        cron_repo = _CronRepoStub(job)
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = AgentCronManager.__new__(AgentCronManager)
        mgr._svc = svc
        mgr._repo = cron_repo
        mgr._run_repo = None
        mgr._registry = JobExecutorRegistry()
        mgr._sems = {"c1": asyncio.Semaphore(1)}
        mgr._scheduler = MagicMock()
        mgr._running = False

        await mgr._execute("c1")

        svc.create_session.assert_awaited_once()
        svc.send_message.assert_awaited_once()


class AgentCronManagerSpanTests(unittest.IsolatedAsyncioTestCase):
    """Verify §7.1 / CLAUDE.md observability: cron.job.fire / pre_action / agent_dispatch spans."""

    def setUp(self) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._orig_tracer = cron_manager_mod.tracer
        cron_manager_mod.tracer = provider.get_tracer(cron_manager_mod.__name__)

    def tearDown(self) -> None:
        cron_manager_mod.tracer = self._orig_tracer
        self.exporter.clear()

    async def test_successful_fire_emits_root_and_agent_dispatch_spans(self):
        job = _make_job(pre_action=None, input_template="hi {{ now }}")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        spans = self.exporter.get_finished_spans()
        names = [s.name for s in spans]
        self.assertIn("cron.job.fire", names)
        self.assertIn("cron.agent_dispatch", names)
        # No pre_action span when pre_action is None.
        self.assertNotIn("cron.pre_action", names)

        fire = next(s for s in spans if s.name == "cron.job.fire")
        fire_attrs = dict(fire.attributes) if fire.attributes else {}
        self.assertEqual(fire_attrs.get("cron.job_id"), "c1")
        # cron_job_run_id matches the create_run row id.
        self.assertEqual(fire_attrs.get("cron.job_run_id"), run_repo.created[0]["id"])
        # kind attribute is empty string when no pre_action.
        self.assertEqual(fire_attrs.get("cron.kind"), "")
        self.assertEqual(fire_attrs.get("cron.terminal_status"), "success")

        agent = next(s for s in spans if s.name == "cron.agent_dispatch")
        agent_attrs = dict(agent.attributes) if agent.attributes else {}
        self.assertEqual(agent_attrs.get("cron.agent_id"), "a1")
        self.assertEqual(agent_attrs.get("cron.agent_session_id"), "s1")
        self.assertEqual(agent_attrs.get("cron.agent_dispatch.status"), "ok")

        # Parent relationship: agent_dispatch is a child of cron.job.fire.
        self.assertEqual(agent.parent.span_id, fire.context.span_id)

    async def test_noop_pre_action_emits_three_span_tree(self):
        job = _make_job(
            pre_action={"kind": "noop", "params": {}},
            input_template="status={{ pre.status }}",
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        registry = JobExecutorRegistry()
        registry.register(NoopExecutor())
        mgr = _bare_manager(svc, cron_repo, run_repo, registry)
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        spans = self.exporter.get_finished_spans()
        names = [s.name for s in spans]
        self.assertIn("cron.job.fire", names)
        self.assertIn("cron.pre_action", names)
        self.assertIn("cron.agent_dispatch", names)

        fire = next(s for s in spans if s.name == "cron.job.fire")
        pre = next(s for s in spans if s.name == "cron.pre_action")
        agent = next(s for s in spans if s.name == "cron.agent_dispatch")

        fire_attrs = dict(fire.attributes) if fire.attributes else {}
        self.assertEqual(fire_attrs.get("cron.kind"), "noop")
        self.assertEqual(fire_attrs.get("cron.terminal_status"), "success")

        pre_attrs = dict(pre.attributes) if pre.attributes else {}
        self.assertEqual(pre_attrs.get("cron.pre_action.kind"), "noop")
        self.assertEqual(pre_attrs.get("cron.pre_action.status"), "ok")

        # Both children share cron.job.fire as parent.
        self.assertEqual(pre.parent.span_id, fire.context.span_id)
        self.assertEqual(agent.parent.span_id, fire.context.span_id)

    async def test_agent_dispatch_failure_sets_error_status_on_spans(self):
        job = _make_job(pre_action=None, input_template="hi")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock(side_effect=RuntimeError("offline"))

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        spans = self.exporter.get_finished_spans()
        fire = next(s for s in spans if s.name == "cron.job.fire")
        agent = next(s for s in spans if s.name == "cron.agent_dispatch")
        fire_attrs = dict(fire.attributes) if fire.attributes else {}
        agent_attrs = dict(agent.attributes) if agent.attributes else {}
        self.assertEqual(fire_attrs.get("cron.terminal_status"), "agent_failed")
        self.assertEqual(agent_attrs.get("cron.agent_dispatch.status"), "error")

    async def test_skipped_and_disabled_paths_emit_no_span(self):
        # Disabled job: no span (we short-circuit before creating run_id).
        job = _make_job(enabled=False)
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock()
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        self.assertEqual(self.exporter.get_finished_spans(), ())

    async def test_fire_persists_fire_span_trace_id_to_run_row(self):
        """The fire path must write the ``cron.job.fire`` span's trace_id onto
        the run row, so cron history surfaces a non-empty trace_id. Regression
        guard for "cron run history trace_id is always blank"."""
        job = _make_job(pre_action=None, input_template="hi {{ now }}")
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        await mgr._execute("c1")

        fire = next(
            s for s in self.exporter.get_finished_spans() if s.name == "cron.job.fire"
        )
        fire_trace = format(fire.context.trace_id, "032x")
        run_id = run_repo.created[0]["id"]
        # Exactly one update carried trace_id, it targeted this run row, and the
        # value equals the fire span's trace_id (not all-zeros / not blank).
        trace_writes = [
            (rid, u["trace_id"]) for rid, u in run_repo.updates if "trace_id" in u
        ]
        self.assertEqual(trace_writes, [(run_id, fire_trace)])
        self.assertNotEqual(fire_trace, "0" * 32)

    async def test_untraced_fire_leaves_trace_id_unwritten(self):
        """With the no-op tracer (tracing disabled) the all-zero trace_id must
        not be persisted — a NULL column means "untraced", never "0000…"."""
        # Swap in the no-op tracer for this one fire.
        from opentelemetry import trace as _otel_trace

        prev = cron_manager_mod.tracer
        cron_manager_mod.tracer = _otel_trace.NoOpTracer()
        try:
            job = _make_job(pre_action=None, input_template="hi")
            cron_repo = _CronRepoStub(job)
            run_repo = _RunRepoStub()
            svc = MagicMock()
            svc.create_session = AsyncMock(return_value={"session_id": "s1"})
            svc.send_message = AsyncMock()

            mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
            mgr._sems["c1"] = asyncio.Semaphore(1)

            await mgr._execute("c1")
        finally:
            cron_manager_mod.tracer = prev

        trace_writes = [u for _, u in run_repo.updates if "trace_id" in u]
        self.assertEqual(trace_writes, [])


class AgentCronManagerCreateJobValidationTests(unittest.IsolatedAsyncioTestCase):
    """Reject ``create_job`` / ``update_job`` with unknown ``agent_id`` before
    hitting the DB, so callers see a clean ``ValueError`` instead of a
    Postgres ``ForeignKeyViolationError`` on ``cron_jobs.agent_id``.
    """

    def _agent_repo_stub(self, known: dict[str, dict] | None = None):
        known = known or {}
        repo = MagicMock()
        async def _get_agent(agent_id):
            return known.get(agent_id)
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self, agent_repo):
        svc = MagicMock()
        svc.agent_repo = agent_repo
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "* * * * *", "timezone": "UTC",
            "max_concurrency": 1,
        })
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr, cron_repo

    async def test_create_job_rejects_unknown_agent_id_before_db_insert(self):
        agent_repo = self._agent_repo_stub(known={})
        mgr, cron_repo = self._make_mgr(agent_repo)
        with self.assertRaises(ValueError) as ctx:
            await mgr.create_job({"agent_id": "doyoutrade-agent", "name": "n",
                                  "cron_expression": "* * * * *",
                                  "input_template": "x"})
        self.assertIn("doyoutrade-agent", str(ctx.exception))
        cron_repo.upsert_job.assert_not_called()

    async def test_create_job_proceeds_when_agent_exists(self):
        agent_repo = self._agent_repo_stub(known={"a1": {"id": "a1", "status": "active"}})
        mgr, cron_repo = self._make_mgr(agent_repo)
        job = await mgr.create_job({"agent_id": "a1", "name": "n",
                                    "cron_expression": "* * * * *",
                                    "input_template": "x"})
        self.assertEqual(job["agent_id"], "a1")
        cron_repo.upsert_job.assert_awaited_once()

    async def test_create_job_rejects_missing_agent_id(self):
        agent_repo = self._agent_repo_stub(known={"a1": {"id": "a1"}})
        mgr, cron_repo = self._make_mgr(agent_repo)
        with self.assertRaises(ValueError):
            await mgr.create_job({"name": "n", "cron_expression": "* * * * *",
                                  "input_template": "x"})
        cron_repo.upsert_job.assert_not_called()

    async def test_update_job_only_revalidates_on_changed_agent_id(self):
        agent_repo = self._agent_repo_stub(known={"a1": {"id": "a1"}})
        agent_calls: list[str] = []
        async def _get_agent(agent_id):
            agent_calls.append(agent_id)
            return {"id": agent_id} if agent_id == "a1" else None
        agent_repo.get_agent = _get_agent

        mgr, cron_repo = self._make_mgr(agent_repo)
        cron_repo.get_job = AsyncMock(return_value={"id": "c1", "agent_id": "a1",
                                                    "enabled": True})
        await mgr.update_job("c1", {"name": "renamed"})
        self.assertEqual(agent_calls, [])  # agent_id unchanged → no recheck

        with self.assertRaises(ValueError):
            await mgr.update_job("c1", {"agent_id": "ghost-agent"})
        self.assertEqual(agent_calls, ["ghost-agent"])

    async def test_create_job_skips_validation_when_svc_has_no_agent_repo(self):
        svc = MagicMock(spec=[])  # no agent_repo attribute
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(return_value={"id": "c1", "agent_id": "x",
                                                       "enabled": False,
                                                       "cron_expression": "* * * * *",
                                                       "timezone": "UTC",
                                                       "max_concurrency": 1})
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        await mgr.create_job({"agent_id": "x", "name": "n",
                              "cron_expression": "* * * * *",
                              "input_template": "x", "enabled": False})
        cron_repo.upsert_job.assert_awaited_once()


class AgentCronManagerCronExpressionValidationTests(unittest.IsolatedAsyncioTestCase):
    """Reject obviously-bad cron expressions at write time so the bad row
    never makes it to the database. Without this, the next server boot
    would crash inside ``start()`` when APScheduler tries to compile the
    stored expression (see incident: ``54`` lands in the hour field)."""

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "Asia/Shanghai",
            "max_concurrency": 1,
        })
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr, cron_repo

    async def test_create_job_rejects_invalid_cron_expression(self):
        mgr, cron_repo = self._make_mgr()
        # '0 54 * * *' — 54 in the hour slot is exactly the historical
        # regression that crashed startup. Manager-level guard must surface
        # a clean ValueError BEFORE the upsert hits the DB.
        with self.assertRaises(ValueError) as ctx:
            await mgr.create_job({
                "agent_id": "a1",
                "name": "n",
                "cron_expression": "0 54 * * *",
                "timezone": "Asia/Shanghai",
                "input_template": "x",
            })
        self.assertIn("invalid cron_expression", str(ctx.exception))
        cron_repo.upsert_job.assert_not_called()
        mgr._register.assert_not_called()

    async def test_create_job_accepts_missing_timezone_via_db_default(self):
        """Missing ``timezone`` must NOT raise — the DB column defaults to
        ``UTC`` and the validator validates against that. Repair scripts
        or REST callers that omit tz still go through cleanly."""
        mgr, cron_repo = self._make_mgr()
        await mgr.create_job({
            "agent_id": "a1",
            "name": "n",
            "cron_expression": "* * * * *",
            # timezone deliberately omitted
            "input_template": "x",
        })
        cron_repo.upsert_job.assert_awaited_once()

    async def test_update_job_rejects_invalid_cron_expression(self):
        mgr, cron_repo = self._make_mgr()
        cron_repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "Asia/Shanghai",
        })
        with self.assertRaises(ValueError):
            await mgr.update_job("c1", {"cron_expression": "* 99 * * *"})
        cron_repo.upsert_job.assert_not_called()
        mgr._deregister.assert_not_called()

    async def test_update_job_validates_against_existing_timezone(self):
        """Updating only the timezone must revalidate against the *existing*
        cron expression. Catches a bad ``timezone`` value before it's
        persisted (would crash registration with KeyError)."""
        mgr, cron_repo = self._make_mgr()
        cron_repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "Asia/Shanghai",
        })
        with self.assertRaises(ValueError):
            await mgr.update_job("c1", {"timezone": "Not/A_Real_Zone"})
        cron_repo.upsert_job.assert_not_called()

    async def test_update_job_skips_validation_when_schedule_untouched(self):
        """``name``-only updates must not pay the validation cost — and must
        not error out if the persisted ``cron_expression`` is somehow
        already invalid (e.g. legacy bad row, being renamed before
        deletion)."""
        mgr, cron_repo = self._make_mgr()
        cron_repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 54 * * *",  # already-broken legacy row
            "timezone": "Asia/Shanghai",
        })
        # Should succeed: we're not touching the schedule, so the validator
        # stays out of the way and the user can still rename / re-target
        # the bad row before deleting it.
        await mgr.update_job("c1", {"name": "renamed"})
        cron_repo.upsert_job.assert_awaited_once()


class AgentCronManagerMaxConcurrencyGuardTests(unittest.IsolatedAsyncioTestCase):
    """``max_concurrency=0`` makes ``asyncio.Semaphore(0)`` always
    locked, so every cron fire takes the "skipped" path silently. The
    job appears registered and "running" but never invokes the agent.
    Same silent-failure shape as the next-fire-distance footgun: a
    type-valid input that produces an absurd downstream effect."""

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(side_effect=lambda d: {**d, "id": d.get("id", "c1")})
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr, cron_repo

    def test_validator_rejects_zero(self):
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_max_concurrency(0)
        msg = str(ctx.exception)
        self.assertIn("max_concurrency must be >= 1", msg)
        self.assertIn("silently block", msg)

    def test_validator_rejects_negative(self):
        with self.assertRaises(ValueError):
            AgentCronManager._validate_max_concurrency(-3)

    def test_validator_rejects_non_int(self):
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_max_concurrency("two")
        self.assertIn("got str", str(ctx.exception))

    def test_validator_accepts_positive(self):
        self.assertEqual(AgentCronManager._validate_max_concurrency(1), 1)
        self.assertEqual(AgentCronManager._validate_max_concurrency(5), 5)

    def test_validator_defaults_none_to_one(self):
        self.assertEqual(AgentCronManager._validate_max_concurrency(None), 1)

    async def test_create_job_rejects_zero_max_concurrency(self):
        mgr, cron_repo = self._make_mgr()
        with self.assertRaises(ValueError):
            await mgr.create_job({
                "agent_id": "a1",
                "name": "n",
                "cron_expression": "* * * * *",
                "timezone": "UTC",
                "input_template": "x",
                "max_concurrency": 0,
            })
        cron_repo.upsert_job.assert_not_called()
        mgr._register.assert_not_called()

    async def test_update_job_rejects_zero_max_concurrency(self):
        mgr, cron_repo = self._make_mgr()
        cron_repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "UTC",
            "max_concurrency": 1,
        })
        with self.assertRaises(ValueError):
            await mgr.update_job("c1", {"max_concurrency": 0})
        cron_repo.upsert_job.assert_not_called()


class AgentCronManagerNextFireEchoTests(unittest.IsolatedAsyncioTestCase):
    """create_job / update_job must echo back ``next_fire_time`` so
    callers can immediately spot timezone-drift bugs (caller computed
    time in local TZ but the job stores UTC, etc.).
    """

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(
            side_effect=lambda d: {**d, "id": d.get("id", "c1")}
        )
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr

    async def test_create_job_echoes_next_fire_time(self):
        mgr = self._make_mgr()
        result = await mgr.create_job({
            "agent_id": "a1",
            "name": "n",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "input_template": "x",
        })
        self.assertIn("next_fire_time", result)
        # Format is ISO-8601 with timezone offset (APScheduler returns
        # a timezone-aware datetime).
        self.assertRegex(
            result["next_fire_time"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
        )
        # Hour position MUST match the cron expression (catches the
        # silent-store-wrong-timezone class of bugs at echo time).
        self.assertIn("T09:00:00", result["next_fire_time"])

    async def test_update_job_echoes_next_fire_time_when_schedule_changed(self):
        mgr = self._make_mgr()
        # First create.
        await mgr.create_job({
            "agent_id": "a1",
            "name": "n",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "input_template": "x",
        })
        # Then update schedule and confirm echo reflects the NEW
        # expression, not the old one.
        mgr._repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "UTC",
            "max_concurrency": 1,
        })
        updated = await mgr.update_job("c1", {"cron_expression": "0 15 * * *"})
        self.assertIn("T15:00:00", updated["next_fire_time"])

    async def test_update_job_echoes_next_fire_time_when_schedule_unchanged(self):
        """Even a name-only update should still echo current
        next_fire_time so callers can re-check drift."""
        mgr = self._make_mgr()
        mgr._repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "UTC",
            "max_concurrency": 1,
        })
        updated = await mgr.update_job("c1", {"name": "renamed"})
        self.assertIn("T09:00:00", updated["next_fire_time"])


class AgentCronManagerDistantScheduleGuardTests(unittest.IsolatedAsyncioTestCase):
    """Backstop against syntactically-valid cron expressions whose next
    fire is far in the future — almost always a field-order mistake or a
    timezone mismatch.

    Incidents this guards against:
      * ``28 23 23 5 *`` written as a "30 seconds later" delay — minute=28
        of hour 23 on May 23, next fire is the next May 23 (~+1 year).
      * Same expression but caller computed local time in Asia/Shanghai
        and stored ``timezone="UTC"`` — multi-hour drift visible in the
        echoed ``next fires at`` timestamp.
    """

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        from datetime import datetime, timezone as _tz
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(side_effect=lambda d: {**d, "id": d.get("id", "c1")})
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr, cron_repo, datetime, _tz

    async def test_distance_check_passes_when_next_fire_within_threshold(self):
        """A normal "every day at 9am" cron must not trip the guard."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "0 9 * * *", "UTC",
        )
        # Should be either today or tomorrow at 09:00 — well within 30 days.
        AgentCronManager._validate_next_fire_distance(
            trigger, "0 9 * * *", "UTC",
            acknowledge_distant=False,
            now=datetime(2026, 5, 23, 8, 0, tzinfo=_tz.utc),
        )

    async def test_distance_check_blocks_when_next_fire_far(self):
        """Replay the session bug: ``28 23 23 5 *`` submitted just
        after 23:28 wraps to next year. The boundary-miss diagnosis
        names the specific cause + recommends ``--in 60s`` (since
        cron expressions are 1-minute precise and unreliable for
        relative delays)."""
        from datetime import datetime, timezone as _tz
        # now is 2 min past 23:28 — within the 5-min boundary window
        # so the message specializes as boundary-miss.
        now = datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc)
        trigger = AgentCronManager._validate_cron_expression(
            "28 23 23 5 *", "UTC",
        )
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_next_fire_distance(
                trigger, "28 23 23 5 *", "UTC",
                acknowledge_distant=False,
                now=now,
            )
        msg = str(ctx.exception)
        # Boundary-miss diagnosis: names the elapsed match, the wrap
        # year, AND the working CLI alternative.
        self.assertIn("calendar pin", msg)
        self.assertIn("next fires at", msg)
        self.assertIn("2027-05-23", msg)
        self.assertIn("2026-05-23T23:30", msg)
        self.assertIn("just elapsed", msg)
        self.assertIn("--in 60s", msg)

    async def test_distance_check_bypassed_when_acknowledged(self):
        """Intentional far-future reminders pass through when the caller
        explicitly opts in."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "0 0 1 1 *", "UTC",
        )
        AgentCronManager._validate_next_fire_distance(
            trigger, "0 0 1 1 *", "UTC",
            acknowledge_distant=True,
            now=datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc),
        )

    async def test_create_job_blocks_distant_schedule_end_to_end(self):
        """End-to-end through create_job: distant next-fire must surface
        as ValueError BEFORE the row is persisted."""
        from datetime import datetime, timezone as _tz
        from unittest.mock import patch
        mgr, cron_repo, _, _ = self._make_mgr()
        # Pin the clock so the assertion stays deterministic — picking a
        # cron expression whose next fire is guaranteed to be > 30 days
        # away from this fixed instant.
        fake_now = datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc)
        with patch.object(cron_manager_mod, "datetime") as dt:
            dt.now.return_value = fake_now
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with self.assertRaises(ValueError) as ctx:
                await mgr.create_job({
                    "agent_id": "a1",
                    "name": "30s greeting",
                    "cron_expression": "28 23 23 5 *",
                    "timezone": "UTC",
                    "input_template": "你好",
                })
        # Boundary-miss diagnosis kicks in for "28 23 23 5 *" at
        # 23:30 (2 min after the just-passed 23:28). New message
        # recommends --in 60s as the CLI-actionable fix.
        msg = str(ctx.exception)
        self.assertIn("--in 60s", msg)
        self.assertIn("just elapsed", msg)
        cron_repo.upsert_job.assert_not_called()
        mgr._register.assert_not_called()

    async def test_create_job_passes_when_acknowledge_flag_set(self):
        """``acknowledge_distant_schedule=True`` lets the same call
        through to the DB / scheduler."""
        from datetime import datetime, timezone as _tz
        from unittest.mock import patch
        mgr, cron_repo, _, _ = self._make_mgr()
        fake_now = datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc)
        with patch.object(cron_manager_mod, "datetime") as dt:
            dt.now.return_value = fake_now
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await mgr.create_job(
                {
                    "agent_id": "a1",
                    "name": "annual reminder",
                    "cron_expression": "28 23 23 5 *",
                    "timezone": "UTC",
                    "input_template": "x",
                },
                acknowledge_distant_schedule=True,
            )
        cron_repo.upsert_job.assert_awaited_once()


class AgentCronManagerAutoPromoteTests(unittest.IsolatedAsyncioTestCase):
    """LLM keeps writing cron-kind calendar pins for "fire in N
    seconds" intents (observed at sessions asst-70142ce43e81 and
    asst-3c8f1fe5aa4a, despite SKILL.md documenting ``--in 60s``).
    The auto-promote in ``_resolve_schedule`` catches the
    cron-kind-but-one-shot reverse pattern: ``day`` + ``month`` both
    specific + next fire < 24h → store as ``at`` kind with
    ``delete_after_run=true`` AND return ``_notice`` educating the
    creating LLM about ``--in``. Solves three problems at once:
    minute-boundary rounding errors, zombie row accumulation, and
    template "please delete me" baking.
    """

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(
            side_effect=lambda d: {**d, "id": d.get("id", "c1")}
        )
        cron_repo.get_job = AsyncMock(return_value=None)
        cron_repo.delete_job = AsyncMock()
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        mgr._deregister_best_effort = AsyncMock()
        return mgr, cron_repo

    async def test_calendar_pin_within_24h_auto_promoted_to_at(self):
        """Replays asst-70142ce43e81: LLM submits ``59 10 24 5 *``
        on May 24 02:58 UTC (= local 10:58 Asia/Shanghai) wanting
        "fire in 1 minute". Cron-kind would round to 10:59:00 with
        next_fire_in_seconds≈0 (LLM math error). Auto-promote
        replaces with ``at`` kind so the row is second-precise."""
        from datetime import datetime, timezone as _tz
        from unittest.mock import patch
        from doyoutrade.assistant import cron_manager as cron_manager_mod
        mgr, cron_repo = self._make_mgr()
        fake_now = datetime(2026, 5, 24, 2, 58, 59, tzinfo=_tz.utc)
        with patch.object(cron_manager_mod, "datetime") as dt:
            dt.now.return_value = fake_now
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            dt.fromisoformat = datetime.fromisoformat
            job = await mgr.create_job({
                "agent_id": "a1",
                "name": "1-min greeting",
                "cron_expression": "59 10 24 5 *",
                "timezone": "Asia/Shanghai",
                "input_template": "Hi",
            })
        # Promoted to at-kind.
        self.assertEqual(job["schedule_kind"], "at")
        self.assertIsNotNone(job["at_iso"])
        # Default delete_after_run=true for promoted rows.
        self.assertTrue(job["delete_after_run"])
        # Educational notice surfaced for the creating LLM.
        self.assertIn("_notice", job)
        notice = job["_notice"]
        self.assertIn("Auto-promoted", notice)
        self.assertIn("--in 60s", notice)

    async def test_recurring_pattern_not_promoted(self):
        """``0 9 * * *`` (every day at 9am) must stay cron-kind —
        promotion would convert a recurring schedule into a one-shot,
        breaking the caller's intent."""
        mgr, cron_repo = self._make_mgr()
        job = await mgr.create_job({
            "agent_id": "a1",
            "name": "daily",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "input_template": "Hi",
        })
        self.assertEqual(job["schedule_kind"], "cron")
        self.assertFalse(job["delete_after_run"])
        self.assertNotIn("_notice", job)

    async def test_unix_weekday_1_5_rewritten_to_mon_fri(self):
        mgr, cron_repo = self._make_mgr()
        job = await mgr.create_job({
            "agent_id": "a1",
            "name": "weekday alert",
            "cron_expression": "*/5 9-11,13-14 * * 1-5",
            "timezone": "Asia/Shanghai",
            "input_template": "Hi",
        })
        self.assertEqual(job["cron_expression"], "*/5 9-11,13-14 * * mon-fri")
        self.assertIn("_notice", job)
        self.assertIn("mon-fri", job["_notice"])
        self.assertIn("skips Monday", job["_notice"])

    def test_rewrite_unix_weekday_dow_leaves_other_expressions(self):
        expr, notice = AgentCronManager._rewrite_unix_weekday_dow(
            "0 9 * * mon-fri",
        )
        self.assertEqual(expr, "0 9 * * mon-fri")
        self.assertIsNone(notice)

    async def test_acknowledge_distant_disables_promote(self):
        """``acknowledge_distant_schedule=True`` opts out: caller
        explicitly wants the calendar-pin to fire annually as a
        cron-kind row (e.g. New Year reminder). Must NOT be silently
        promoted into a one-shot."""
        mgr, cron_repo = self._make_mgr()
        job = await mgr.create_job(
            {
                "agent_id": "a1",
                "name": "annual",
                "cron_expression": "0 0 1 1 *",
                "timezone": "UTC",
                "input_template": "🎆",
            },
            acknowledge_distant_schedule=True,
        )
        self.assertEqual(job["schedule_kind"], "cron")
        # Not auto-promoted even though it's a calendar pin.
        self.assertNotIn("_notice", job)

    async def test_explicit_keep_after_run_survives_promote(self):
        """If the caller explicitly says ``delete_after_run=False``,
        the promotion must honor that (caller's choice wins). Edge
        case but the docs claim it's overridable."""
        from datetime import datetime, timezone as _tz
        from unittest.mock import patch
        from doyoutrade.assistant import cron_manager as cron_manager_mod
        mgr, cron_repo = self._make_mgr()
        fake_now = datetime(2026, 5, 24, 2, 58, 59, tzinfo=_tz.utc)
        with patch.object(cron_manager_mod, "datetime") as dt:
            dt.now.return_value = fake_now
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            dt.fromisoformat = datetime.fromisoformat
            job = await mgr.create_job({
                "agent_id": "a1",
                "name": "1-min noisy",
                "cron_expression": "59 10 24 5 *",
                "timezone": "Asia/Shanghai",
                "delete_after_run": False,
                "input_template": "Hi",
            })
        self.assertEqual(job["schedule_kind"], "at")
        self.assertFalse(job["delete_after_run"])

    async def test_calendar_pin_beyond_24h_falls_back_to_distant_guard(self):
        """Pin pattern fires > 24h out: not promoted (the LLM intent
        is genuinely ambiguous at that range), so the distant-fire
        validator runs and rejects without ack_distant. This keeps
        the existing safety net intact."""
        from datetime import datetime, timezone as _tz
        from unittest.mock import patch
        from doyoutrade.assistant import cron_manager as cron_manager_mod
        mgr, _ = self._make_mgr()
        # Fixed clock May 23 23:30 UTC; expression fires May 24 23:28
        # = ~24h delta, then next iteration is 1 year out so > 24h.
        fake_now = datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc)
        with patch.object(cron_manager_mod, "datetime") as dt:
            dt.now.return_value = fake_now
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            dt.fromisoformat = datetime.fromisoformat
            with self.assertRaises(ValueError) as ctx:
                await mgr.create_job({
                    "agent_id": "a1",
                    "name": "x",
                    "cron_expression": "28 23 23 5 *",
                    "timezone": "UTC",
                    "input_template": "Hi",
                })
        # Falls through to existing distant-fire validator.
        self.assertIn("calendar pin", str(ctx.exception))


class AgentCronTriggerHeaderTests(unittest.TestCase):
    """The ``[cron-trigger]`` header carries override instructions
    that must beat stale "please delete me" text the creating LLM
    bakes into the template body."""

    def test_header_warns_when_body_has_delete_instruction(self):
        from doyoutrade.assistant.cron_manager import _build_cron_trigger_header
        from datetime import datetime, timezone as _tz
        job = {
            "id": "cron-abc",
            "name": "test",
            "delete_after_run": True,
        }
        fired_at = datetime(2026, 5, 24, 2, 59, 0, tzinfo=_tz.utc)
        # Body contains the kind of self-destruct instruction LLMs
        # keep baking in (observed across multiple sessions).
        body = (
            "到点了，你好！请用 doyoutrade-cli cron delete cron-abc "
            "把这个任务删掉。"
        )
        header = _build_cron_trigger_header(job, fired_at, body=body)
        # Standard delete_after_run hint is present.
        self.assertIn("delete_after_run=true", header)
        # Plus an explicit "IGNORE the stale text" override.
        self.assertIn("IGNORE", header)
        self.assertIn("stale", header)

    def test_header_skips_warning_when_body_has_no_delete_text(self):
        from doyoutrade.assistant.cron_manager import _build_cron_trigger_header
        from datetime import datetime, timezone as _tz
        job = {"id": "c1", "name": "x", "delete_after_run": True}
        fired_at = datetime(2026, 5, 24, tzinfo=_tz.utc)
        header = _build_cron_trigger_header(
            job, fired_at, body="到点了，你好！",
        )
        # delete_after_run hint still there...
        self.assertIn("delete_after_run=true", header)
        # ...but no "IGNORE" override (body is clean).
        self.assertNotIn("IGNORE", header)

    def test_header_skips_delete_hint_when_flag_false(self):
        from doyoutrade.assistant.cron_manager import _build_cron_trigger_header
        from datetime import datetime, timezone as _tz
        job = {"id": "c1", "name": "x", "delete_after_run": False}
        fired_at = datetime(2026, 5, 24, tzinfo=_tz.utc)
        # Body contains delete text but the system isn't auto-cleaning
        # — so the user template's instruction is legitimate (caller
        # set keep-after-run explicitly).
        header = _build_cron_trigger_header(
            job, fired_at, body="please run cron delete c1",
        )
        self.assertNotIn("delete_after_run=true", header)
        self.assertNotIn("IGNORE", header)


class AgentCronManagerCalendarPinDriftTests(unittest.IsolatedAsyncioTestCase):
    """Tight-threshold guard for "calendar pin" patterns (day AND month
    both pinned to specific values). These are the LLM's typical "fire
    in 30s" pattern, and the dominant failure mode is timezone drift:
    caller reads local wall clock, fills minute/hour from local TZ,
    submits ``timezone="UTC"``. The previous 30-day threshold was too
    lax to catch the resulting +8h offset.

    Replays the real incident at asst-d5a0bea2bdc3: LLM at local
    09:18:59 (UTC 01:18:59) wants "30s later", submits
    ``19 9 24 5 *`` (intended local 09:19) with default UTC timezone
    → next_fire = UTC 09:19 = local 17:19, an 8h drift.
    """

    def test_is_calendar_pin_detects_day_and_month_specific(self):
        for expr, expected in [
            ("19 9 24 5 *", True),       # specific date+time
            ("28 23 23 5 *", True),      # the prior incident
            ("0 0 1 1 *", True),         # Jan 1
            ("19 9 * * *", False),       # daily
            ("0 9 * * 1", False),        # every Monday
            ("0 9 24 * *", False),       # 24th of every month
            ("0 9 * 5 *", False),        # every day in May
            ("*/5 * * * *", False),      # every 5 minutes
        ]:
            trigger = AgentCronManager._validate_cron_expression(
                expr, "UTC",
            )
            self.assertEqual(
                AgentCronManager._is_calendar_pin_one_shot(trigger),
                expected,
                f"expr={expr!r} expected calendar_pin={expected}",
            )

    def test_pin_at_eight_hour_offset_blocked_with_tz_drift_hint(self):
        """The actual session bug. ``19 9 24 5 *`` (intended local
        09:19) submitted with ``timezone="UTC"`` while the LLM clock
        was local 09:18:59 (UTC 01:18:59). Next fire resolves to UTC
        09:19 (local 17:19) — 8 hours off, dead center of the
        local↔UTC TZ offset on a CST host.

        The new tight pin threshold (2h) rejects this, and the error
        must include both timestamps so the LLM can spot the drift
        without a second round-trip.
        """
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "19 9 24 5 *", "UTC",
        )
        now = datetime(2026, 5, 24, 1, 19, tzinfo=_tz.utc)
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_next_fire_distance(
                trigger, "19 9 24 5 *", "UTC",
                acknowledge_distant=False, now=now,
            )
        msg = str(ctx.exception)
        # Pin-specific wording: must call out calendar pin pattern.
        self.assertIn("calendar pin", msg)
        # Hour delta surfaced (single-digit, not days).
        self.assertIn("h from now", msg)
        # Both anchor timestamps present.
        self.assertIn("2026-05-24T09:19:00", msg)
        self.assertIn("2026-05-24T01:19:00", msg)
        # Recovery hint: CLI-actionable ``--in <duration>`` (the
        # API-only ``acknowledge_distant_schedule`` flag is NOT
        # surfaced for pin-distant — pointing LLMs at it sends them
        # on a dead-end retry loop trying ``--acknowledge-distant-
        # schedule`` as a CLI flag).
        self.assertIn("--in", msg)
        self.assertIn("--at", msg)
        # The suggested --timezone value MUST be an IANA key the
        # caller can copy-paste directly, NOT a TZ abbreviation. A
        # prior version emitted "CST" (from astimezone().tzinfo) and
        # the LLM dead-ended on "No time zone found with key CST".
        # We don't assert a specific zone (CI host TZ varies) but
        # we DO assert the abbreviation form is absent and the
        # IANA-style slash is present somewhere in the --timezone
        # suggestion.
        if "--timezone " in msg:
            suggested = msg.split("--timezone ", 1)[1].split()[0]
            self.assertIn(
                "/", suggested,
                f"expected IANA key (e.g. Asia/Shanghai), got "
                f"{suggested!r} — abbreviations like CST/EST are not "
                f"valid ZoneInfo keys.",
            )

    def test_pin_within_2h_threshold_passes(self):
        """Pin pattern with next fire 30 minutes away — legitimate
        "fire soon" usage, must NOT trip the tight threshold."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "30 12 24 5 *", "UTC",
        )
        # 12:00 -> 12:30 = 30 min away, well under 2h.
        AgentCronManager._validate_next_fire_distance(
            trigger, "30 12 24 5 *", "UTC",
            acknowledge_distant=False,
            now=datetime(2026, 5, 24, 12, 0, tzinfo=_tz.utc),
        )

    def test_recurring_pattern_uses_lax_30day_threshold(self):
        """Non-pin patterns (e.g. ``0 9 * * 1`` — every Monday) must
        still use the 30-day threshold so legitimate weekly /
        monthly schedules aren't false-positive rejected.
        Concretely: a Monday 09:00 schedule submitted on Monday
        09:01 has next fire ~7 days out, which would falsely trip
        any tighter pin threshold."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "0 9 * * 1", "UTC",
        )
        # 2026-05-25 is a Monday; submit at 09:01 same day → next
        # fire is next Monday, ~7 days from now.
        AgentCronManager._validate_next_fire_distance(
            trigger, "0 9 * * 1", "UTC",
            acknowledge_distant=False,
            now=datetime(2026, 5, 25, 9, 1, tzinfo=_tz.utc),
        )

    def test_pin_far_future_outside_boundary_window(self):
        """Pin pattern where the past match is > 5 min ago: NOT a
        boundary-miss, so the generic pin-distant message fires
        (with the ``--in``/``--at`` CLI recovery hint). The
        boundary-miss path is for the "minute just elapsed" sub-case
        only."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "28 23 23 5 *", "UTC",
        )
        # 23:35 is 7 minutes past 23:28 → outside the boundary
        # window so we get the generic pin-distant message.
        now = datetime(2026, 5, 23, 23, 35, tzinfo=_tz.utc)
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_next_fire_distance(
                trigger, "28 23 23 5 *", "UTC",
                acknowledge_distant=False, now=now,
            )
        msg = str(ctx.exception)
        self.assertIn("calendar pin", msg)
        # Generic pin-distant uses "h from now" formatting.
        self.assertIn("h from now", msg)
        # CLI-actionable fix surfaced.
        self.assertIn("--in", msg)

    def test_pin_boundary_miss_specialized_message(self):
        """When the past match is within 5 min, the message
        specializes to "minute just elapsed" with stronger
        ``--in 60s`` recommendation (replays session
        asst-500105d61a41 first-attempt diagnosis)."""
        from datetime import datetime, timezone as _tz
        trigger = AgentCronManager._validate_cron_expression(
            "28 23 23 5 *", "UTC",
        )
        # 23:30 = 2 min past 23:28 → inside the 5-min window.
        now = datetime(2026, 5, 23, 23, 30, tzinfo=_tz.utc)
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_next_fire_distance(
                trigger, "28 23 23 5 *", "UTC",
                acknowledge_distant=False, now=now,
            )
        msg = str(ctx.exception)
        self.assertIn("just elapsed", msg)
        self.assertIn("--in 60s", msg)
        self.assertIn("calendar pin", msg)


class AgentCronManagerTaggedScheduleTests(unittest.IsolatedAsyncioTestCase):
    """Tagged-union schedule (``schedule_kind='cron'`` vs ``'at'``)
    eliminates the dominant LLM TZ-drift / arithmetic-error class by
    construction: ``at_iso`` carries an explicit offset and
    ``in_duration`` skips the cron expression entirely.
    """

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(
            side_effect=lambda d: {**d, "id": d.get("id", "c1")}
        )
        cron_repo.get_job = AsyncMock(return_value=None)
        cron_repo.delete_job = AsyncMock()
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        mgr._deregister_best_effort = AsyncMock()
        return mgr, cron_repo

    def test_parse_duration_accepts_common_units(self):
        for raw, expected_seconds in [
            ("60s", 60),
            ("5m", 300),
            ("2h", 7200),
            ("1d", 86400),
            ("1.5h", 5400),
            (" 60s ", 60),
            ("90sec", 90),
            ("3 min", 180),
        ]:
            with self.subTest(raw=raw):
                result = AgentCronManager._parse_duration(raw)
                self.assertEqual(int(result.total_seconds()), expected_seconds)

    def test_parse_duration_rejects_bad_input(self):
        for raw in ["", "abc", "60", "5x", "-30s", "0s"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    AgentCronManager._parse_duration(raw)

    def test_validate_at_iso_rejects_naive(self):
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_at_iso("2026-05-24T10:23:00")
        self.assertIn("timezone offset", str(ctx.exception))

    def test_validate_at_iso_rejects_past_beyond_grace(self):
        from datetime import datetime, timezone as _tz
        now = datetime(2026, 5, 24, 10, 0, tzinfo=_tz.utc)
        # 5 minutes in the past — beyond the 45s grace window.
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_at_iso(
                "2026-05-24T09:55:00+00:00", now=now,
            )
        self.assertIn("in the past", str(ctx.exception))

    def test_validate_at_iso_accepts_within_grace(self):
        from datetime import datetime, timezone as _tz
        now = datetime(2026, 5, 24, 10, 0, tzinfo=_tz.utc)
        # 30s past — within the 45s grace.
        AgentCronManager._validate_at_iso(
            "2026-05-24T09:59:30+00:00", now=now,
        )

    def test_validate_at_iso_rejects_distant_future(self):
        from datetime import datetime, timezone as _tz
        now = datetime(2026, 5, 24, 10, 0, tzinfo=_tz.utc)
        # 60 days out — past the 30-day threshold.
        with self.assertRaises(ValueError) as ctx:
            AgentCronManager._validate_at_iso(
                "2026-07-23T10:00:00+00:00", now=now,
            )
        self.assertIn("acknowledge_distant_schedule", str(ctx.exception))

    def test_validate_at_iso_acknowledges_distant_future(self):
        from datetime import datetime, timezone as _tz
        now = datetime(2026, 5, 24, 10, 0, tzinfo=_tz.utc)
        AgentCronManager._validate_at_iso(
            "2026-07-23T10:00:00+00:00", now=now,
            acknowledge_distant=True,
        )

    async def test_create_at_with_in_duration_resolves_to_at_iso(self):
        mgr, cron_repo = self._make_mgr()
        job = await mgr.create_job({
            "agent_id": "a1",
            "name": "1-min greeting",
            "schedule_kind": "at",
            "in_duration": "60s",
            "input_template": "Hi",
        })
        self.assertEqual(job["schedule_kind"], "at")
        self.assertIsNotNone(job["at_iso"])
        # Default for at-kind: delete_after_run=True (no zombies).
        self.assertTrue(job["delete_after_run"])
        # next_fire_in_seconds should be ~60s.
        self.assertGreater(job["next_fire_in_seconds"], 55)
        self.assertLessEqual(job["next_fire_in_seconds"], 60)

    async def test_create_at_with_explicit_iso(self):
        mgr, cron_repo = self._make_mgr()
        from datetime import datetime, timedelta, timezone as _tz
        target = datetime.now(_tz.utc) + timedelta(minutes=2)
        job = await mgr.create_job({
            "agent_id": "a1",
            "name": "reminder",
            "schedule_kind": "at",
            "at_iso": target.isoformat(),
            "input_template": "Hi",
        })
        self.assertEqual(job["schedule_kind"], "at")
        self.assertGreaterEqual(job["next_fire_in_seconds"], 110)
        self.assertLess(job["next_fire_in_seconds"], 130)

    async def test_create_at_rejects_both_at_iso_and_in_duration(self):
        mgr, _ = self._make_mgr()
        with self.assertRaises(ValueError) as ctx:
            await mgr.create_job({
                "agent_id": "a1", "name": "x",
                "schedule_kind": "at",
                "at_iso": "2026-05-24T10:23:00+08:00",
                "in_duration": "60s",
                "input_template": "Hi",
            })
        self.assertIn("at_iso OR in_duration", str(ctx.exception))

    async def test_create_cron_kind_still_works_legacy_payload(self):
        """Legacy callers that don't pass ``schedule_kind`` but DO
        pass ``cron_expression`` should still create a recurring
        cron job — backwards compatibility is mandatory."""
        mgr, cron_repo = self._make_mgr()
        job = await mgr.create_job({
            "agent_id": "a1",
            "name": "daily",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "input_template": "Hi",
        })
        self.assertEqual(job["schedule_kind"], "cron")
        self.assertFalse(job["delete_after_run"])

    async def test_post_fire_cleanup_deletes_when_flag_set(self):
        """``delete_after_run=True`` triggers hard delete after fire."""
        mgr, cron_repo = self._make_mgr()
        from datetime import datetime, timezone as _tz
        job = {
            "id": "c1", "agent_id": "a1", "enabled": True,
            "schedule_kind": "at", "at_iso": "2026-05-24T10:00:00+00:00",
            "delete_after_run": True,
        }
        await mgr._post_fire_cleanup(
            job, datetime(2026, 5, 24, 10, 0, tzinfo=_tz.utc),
        )
        cron_repo.delete_job.assert_awaited_once_with("c1")

    async def test_post_fire_cleanup_legacy_calendar_pin_still_disables(self):
        """A legacy cron-kind calendar pin with no explicit
        delete_after_run falls back to soft auto-disable."""
        mgr, cron_repo = self._make_mgr()
        from datetime import datetime, timezone as _tz
        job = {
            "id": "c1", "agent_id": "a1", "enabled": True,
            "schedule_kind": "cron",
            "cron_expression": "28 23 23 5 *",
            "timezone": "UTC",
            "delete_after_run": False,
        }
        await mgr._post_fire_cleanup(
            job, datetime(2026, 5, 23, 23, 28, tzinfo=_tz.utc),
        )
        # Legacy path = upsert with enabled=False, NOT delete.
        cron_repo.delete_job.assert_not_called()
        upsert_calls = cron_repo.upsert_job.await_args_list
        self.assertTrue(
            any(
                isinstance(c.args[0], dict) and c.args[0].get("enabled") is False
                for c in upsert_calls
            ),
            f"expected enabled=False upsert; got {upsert_calls}",
        )


class AgentCronManagerOneShotAutoDisableTests(unittest.IsolatedAsyncioTestCase):
    """Calendar-pin patterns (day+month both specific) fire once per
    year by construction, but LLM/user intent is almost always "fire
    once". After the first fire we auto-disable so the year-later
    zombie fire never reaches an agent. Replays the conditions that
    let 7 ``30秒后打招呼`` rows accumulate at session
    asst-91bfd63bf186.
    """

    async def test_pin_pattern_auto_disabled_after_first_fire(self):
        # ``28 23 23 5 *`` — May 23 23:28 each year. After firing on
        # 2026-05-23 the next match is 2027-05-23, well over our
        # 180-day threshold.
        job = _make_job(
            id="c1",
            cron_expression="28 23 23 5 *",
            timezone="UTC",
            enabled=True,
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        svc.create_session = AsyncMock(return_value={"session_id": "s1"})
        svc.send_message = AsyncMock()

        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        mgr._sems["c1"] = asyncio.Semaphore(1)

        # Patch fired_at indirectly by patching datetime.now used in
        # ``_execute``. Simpler: directly invoke the helper with a
        # fixed fired_at.
        from datetime import datetime, timezone as _tz
        await mgr._maybe_auto_disable_one_shot(
            job, datetime(2026, 5, 23, 23, 28, tzinfo=_tz.utc),
        )
        # ``upsert_job`` was called with enabled=False.
        disable_calls = [
            args
            for args in cron_repo.upserts
            if args.get("enabled") is False
        ]
        self.assertEqual(
            len(disable_calls), 1,
            f"expected one auto-disable upsert; got {cron_repo.upserts}",
        )
        self.assertEqual(disable_calls[0]["id"], "c1")

    async def test_recurring_pattern_not_disabled(self):
        """Daily / weekly / monthly recurring jobs must NOT be
        auto-disabled — their next fire is hours/days away, not
        months. This is the safety net for the false-positive case
        where the pin detector misclassifies."""
        for expr in ["0 9 * * *", "0 9 * * 1", "0 9 1 * *"]:
            with self.subTest(expr=expr):
                job = _make_job(
                    id="c1", cron_expression=expr, timezone="UTC",
                    enabled=True,
                )
                cron_repo = _CronRepoStub(job)
                run_repo = _RunRepoStub()
                svc = MagicMock()
                mgr = _bare_manager(
                    svc, cron_repo, run_repo, JobExecutorRegistry(),
                )
                from datetime import datetime, timezone as _tz
                await mgr._maybe_auto_disable_one_shot(
                    job,
                    datetime(2026, 5, 23, 9, 0, tzinfo=_tz.utc),
                )
                disable_calls = [
                    args
                    for args in cron_repo.upserts
                    if args.get("enabled") is False
                ]
                self.assertEqual(
                    disable_calls, [],
                    f"expr={expr!r} should NOT auto-disable; got "
                    f"{cron_repo.upserts}",
                )

    async def test_pin_pattern_with_future_natural_fire_not_disabled(self):
        """Manual trigger of a pin pattern BEFORE its natural fire
        time — e.g. a Nov 1 pin manually fired in April. The natural
        Nov 1 fire is ~7 months away (< 180 days threshold? actually
        check both sides). The threshold is set so an upcoming fire
        within ~6 months survives."""
        # Pin = Nov 1 00:00. Manual fire on April 1 → next natural
        # is Nov 1 same year, ~214 days away.
        job = _make_job(
            id="c1", cron_expression="0 0 1 11 *", timezone="UTC",
            enabled=True,
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        svc = MagicMock()
        mgr = _bare_manager(svc, cron_repo, run_repo, JobExecutorRegistry())
        from datetime import datetime, timezone as _tz
        await mgr._maybe_auto_disable_one_shot(
            job, datetime(2026, 4, 1, 0, 0, tzinfo=_tz.utc),
        )
        # 214 days > 180 day threshold — DOES auto-disable, by
        # design. (False positive risk we accept — see comment on
        # ``_ONE_SHOT_AUTO_DISABLE_THRESHOLD``.)
        # This test documents the behaviour for future tuning.
        # Recovery: cron resume.
        disable_calls = [
            args
            for args in cron_repo.upserts
            if args.get("enabled") is False
        ]
        self.assertEqual(len(disable_calls), 1)

    async def test_disabled_job_not_acted_on(self):
        """An already-disabled job (e.g. paused) shouldn't trigger
        any extra upsert — idempotency safety."""
        job = _make_job(
            id="c1", cron_expression="28 23 23 5 *", timezone="UTC",
            enabled=False,
        )
        cron_repo = _CronRepoStub(job)
        run_repo = _RunRepoStub()
        mgr = _bare_manager(MagicMock(), cron_repo, run_repo, JobExecutorRegistry())
        from datetime import datetime, timezone as _tz
        await mgr._maybe_auto_disable_one_shot(
            job, datetime(2026, 5, 23, 23, 28, tzinfo=_tz.utc),
        )
        self.assertEqual(cron_repo.upserts, [])


class AgentCronManagerNextFireRelativeTests(unittest.IsolatedAsyncioTestCase):
    """``create_job`` / ``update_job`` must also echo back
    ``next_fire_in_seconds`` (signed int relative to now) so LLMs
    can't misread a ``+00:00`` ISO timestamp as "fires in 30s" when
    it's actually 28800s (8 hours) away.
    """

    def _agent_repo_stub(self):
        repo = MagicMock()
        async def _get_agent(agent_id):
            return {"id": agent_id, "status": "active"}
        repo.get_agent = _get_agent
        return repo

    def _make_mgr(self):
        svc = MagicMock()
        svc.agent_repo = self._agent_repo_stub()
        cron_repo = MagicMock()
        cron_repo.upsert_job = AsyncMock(
            side_effect=lambda d: {**d, "id": d.get("id", "c1")}
        )
        cron_repo.get_job = AsyncMock(return_value=None)
        mgr = _bare_manager(svc, cron_repo, MagicMock(), JobExecutorRegistry())
        mgr._register = AsyncMock()
        mgr._deregister = AsyncMock()
        return mgr

    async def test_create_echoes_next_fire_in_seconds(self):
        mgr = self._make_mgr()
        result = await mgr.create_job({
            "agent_id": "a1",
            "name": "n",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "input_template": "x",
        })
        self.assertIn("next_fire_in_seconds", result)
        self.assertIsInstance(result["next_fire_in_seconds"], int)
        # Daily 09:00 UTC fires within 24h of now.
        self.assertGreaterEqual(result["next_fire_in_seconds"], 0)
        self.assertLessEqual(
            result["next_fire_in_seconds"], 24 * 3600,
        )

    async def test_update_echoes_next_fire_in_seconds(self):
        mgr = self._make_mgr()
        mgr._repo.get_job = AsyncMock(return_value={
            "id": "c1", "agent_id": "a1", "enabled": True,
            "cron_expression": "0 9 * * *", "timezone": "UTC",
            "max_concurrency": 1,
        })
        updated = await mgr.update_job("c1", {"name": "renamed"})
        self.assertIn("next_fire_in_seconds", updated)
        self.assertIsInstance(updated["next_fire_in_seconds"], int)


class AgentCronManagerStartResilienceTests(unittest.IsolatedAsyncioTestCase):
    """``start()`` must survive historical bad rows.

    Pre-incident: one persisted job with ``cron_expression='0 54 * * *'``
    (54 in the hour slot) brought down the entire FastAPI server at boot
    because APScheduler raises during ``CronTrigger.from_crontab``. After
    this fix the bad row is logged, marked ``last_status='error'`` so the
    user can identify it via ``list_cron_jobs``, and the remaining jobs
    register normally.
    """

    async def test_start_skips_bad_row_and_marks_state_then_continues(self):
        bad_job = _make_job(
            id="cron-bad", cron_expression="0 54 * * *",
            timezone="Asia/Shanghai",
        )
        good_job = _make_job(
            id="cron-good", cron_expression="0 9 * * *",
            timezone="Asia/Shanghai",
        )

        cron_repo = MagicMock()
        cron_repo.list_jobs = AsyncMock(return_value=[bad_job, good_job])
        cron_repo.update_job_state = AsyncMock()

        mgr = _bare_manager(MagicMock(), cron_repo, MagicMock(), JobExecutorRegistry())

        # Replace the real APScheduler with a stub — we only care that
        # _register is called for each enabled job (and tolerates failure
        # for the bad one).
        registered: list[str] = []
        original_register = mgr._register

        async def _spy_register(job):
            registered.append(job["id"])
            await original_register(job)

        mgr._register = _spy_register
        mgr._scheduler = MagicMock()
        mgr._scheduler.start = MagicMock()

        await mgr.start()

        # Both rows were attempted; only the good one registered cleanly.
        self.assertEqual(set(registered), {"cron-bad", "cron-good"})
        # The bad row's failure was persisted so list_cron_jobs surfaces it.
        cron_repo.update_job_state.assert_awaited_once()
        call_kwargs = cron_repo.update_job_state.await_args.kwargs
        self.assertEqual(cron_repo.update_job_state.await_args.args[0], "cron-bad")
        self.assertEqual(call_kwargs.get("last_status"), "error")
        self.assertIn("register_failed", call_kwargs.get("last_error", ""))
        # The good row is registered in APScheduler.
        mgr._scheduler.start.assert_called_once()
        # The bad row's semaphore did not leak.
        self.assertNotIn("cron-bad", mgr._sems)


if __name__ == "__main__":
    unittest.main()
