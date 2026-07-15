"""E2E: cron task-pipeline (`JobTaskExecutor`) propagates run_id and pushes
the agent's reply to the user's session.

Exercises the surviving task pipeline:

  * ``agent_chat_reply`` — the agent composes a reply that lands as a
    ``role=assistant`` message on ``target_session_id`` with
    ``metadata.source='cron'``. No strategy tick.

The assertion goes through the real cron_manager → JobTaskRegistry
dispatch path, so the cron_job_runs row also captures ``cron_task_kind``
and ``delivery_status``.

Strategy cron task_kinds (``strategy_signal_alert`` / ``strategy_cycle``)
are retired — strategy execution is scheduled via Task Triggers, covered
by ``tests/e2e/test_task_trigger_cycle.py``.
"""
from __future__ import annotations

import asyncio
import unittest

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.assistant.cron_executors import (
    AgentChatReplyExecutor,
    JobExecutorRegistry,
    JobTaskRegistry,
    NoopExecutor,
)
from doyoutrade.assistant.cron_manager import AgentCronManager
from doyoutrade.persistence.repositories import (
    SqlAlchemyCronJobRepository,
    SqlAlchemyCronJobRunRepository,
)


_TERMINAL_STATUSES = {"success", "pre_failed", "agent_failed", "error", "skipped"}


async def _wait_for_terminal(
    cron_run_repo: SqlAlchemyCronJobRunRepository,
    run_id: str,
    *,
    timeout_s: float = 15.0,
) -> dict:
    """Poll the cron run row until it reaches a terminal status or time out."""

    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        run = await cron_run_repo.get_run(run_id)
        if run and run["status"] in _TERMINAL_STATUSES:
            return run
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"cron run did not finish: {run_id} "
                f"status={(run or {}).get('status', 'missing')}"
            )
        await asyncio.sleep(0.1)


def _build_cron_manager(ctx, task_registry: JobTaskRegistry) -> tuple[
    AgentCronManager,
    SqlAlchemyCronJobRepository,
    SqlAlchemyCronJobRunRepository,
]:
    """Construct cron_manager with the legacy pre_action + new task registries.

    The e2e bootstrap doesn't wire cron_manager (only api/server.py does),
    so each E2E case builds it from the same primitives. The legacy
    pre_action registry carries only ``NoopExecutor`` now that the strategy
    cron executors are retired.
    """

    session_factory = ctx.runtime["session_factory"]
    cron_repo = SqlAlchemyCronJobRepository(session_factory)
    cron_run_repo = SqlAlchemyCronJobRunRepository(session_factory)

    legacy = JobExecutorRegistry()
    legacy.register(NoopExecutor())

    mgr = AgentCronManager(
        assistant_service=ctx.runtime["assistant_service"],
        cron_repo=cron_repo,
        cron_run_repo=cron_run_repo,
        executor_registry=legacy,
        task_registry=task_registry,
        timezone="UTC",
    )
    return mgr, cron_repo, cron_run_repo


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class CronTaskPipelineE2E(unittest.IsolatedAsyncioTestCase):
    async def test_agent_chat_reply_pushes_to_target_session(self) -> None:
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            assistant_service = ctx.runtime["assistant_service"]

            agent = await assistant_service.agent_repo.create_agent({
                "name": "E2E Cron Chat Agent",
                "system_prompt": "agent for e2e cron agent_chat_reply",
                "status": "active",
            })

            # The user's session — this is where the cron fire must push
            # its reply as a ``role=assistant`` message.
            target_session = await assistant_service.create_session(
                agent_id=agent["id"],
                title="user chat",
            )
            target_session_id = target_session["session_id"]
            baseline = await assistant_service.repository.list_messages(
                target_session_id, limit=200, offset=0,
            )
            baseline_ids = {m["message_id"] for m in baseline}

            task_registry = JobTaskRegistry()
            cron_job_repo_for_executor = SqlAlchemyCronJobRepository(
                ctx.runtime["session_factory"],
            )
            task_registry.register(
                AgentChatReplyExecutor(
                    assistant_service=assistant_service,
                    cron_job_repository=cron_job_repo_for_executor,
                )
            )

            mgr, _, cron_run_repo = _build_cron_manager(ctx, task_registry)
            await mgr.start()
            try:
                job = await mgr.create_job({
                    "agent_id": agent["id"],
                    "name": "e2e-agent-chat-reply",
                    "cron_expression": "0 0 * * *",
                    "timezone": "UTC",
                    "max_concurrency": 1,
                    "timeout_seconds": 120,
                    "enabled": True,
                    "task_kind": "agent_chat_reply",
                    "task_params_json": {
                        "user_request": "1 分钟后跟我说你好",
                        "target_session_id": target_session_id,
                        "agent_id": agent["id"],
                    },
                })

                run_id = await mgr.trigger_job(job["id"])
                run = await _wait_for_terminal(cron_run_repo, run_id)
            finally:
                await mgr.stop()

            # ── Cron run bookkeeping ────────────────────────────────────────
            self.assertEqual(run["status"], "success", run)
            self.assertEqual(run["cron_task_kind"], "agent_chat_reply", run)
            self.assertEqual(run["delivery_status"], "delivered", run)
            self.assertIsNotNone(run["agent_session_id"], run)
            self.assertIsNone(run["agent_error"], run)

            # The agent's own composition session (a fresh one with
            # title=[Cron]) must be distinct from the user's session — the
            # whole point of the framing is to avoid agent-talks-to-itself.
            self.assertNotEqual(run["agent_session_id"], target_session_id)

            # ── User-side delivery ──────────────────────────────────────────
            after = await assistant_service.repository.list_messages(
                target_session_id, limit=200, offset=0,
            )
            new_messages = [m for m in after if m["message_id"] not in baseline_ids]
            self.assertEqual(
                len(new_messages), 1,
                f"expected exactly one cron push on target session, got "
                f"{[m['message_id'] for m in new_messages]}",
            )
            pushed = new_messages[0]
            self.assertEqual(pushed["role"], "assistant", pushed)
            meta = pushed.get("metadata") or {}
            self.assertEqual(meta.get("source"), "cron", pushed)
            self.assertEqual(meta.get("cron_job_id"), job["id"], pushed)
            self.assertEqual(meta.get("cron_job_run_id"), run_id, pushed)
            self.assertEqual(meta.get("cron_task_kind"), "agent_chat_reply", pushed)
            self.assertIsInstance(pushed.get("content"), str)
            self.assertGreater(len(pushed["content"].strip()), 0)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
