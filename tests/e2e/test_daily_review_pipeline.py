"""E2E: ``daily_review`` cron task-pipeline.

Drives the real cron_manager → JobTaskRegistry dispatch for the new
``daily_review`` kind: a fire pre-gathers the account statement (here a
deterministic injected provider, since the isolated profile has no live QMT),
composes via the stub model, persists the review to the private KB
``journal/`` partition, and pushes it to the user's session as a
``role=assistant`` message with ``metadata.source='cron'``. The cron_job_runs
row captures ``cron_task_kind`` + ``delivery_status``.

DOYOUTRADE_HOME is redirected to the e2e tempdir by the harness, so the journal
write lands in an isolated knowledge base (never the developer's real KB).
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.assistant.cron_executors import (
    DailyReviewExecutor,
    JobExecutorRegistry,
    JobTaskRegistry,
    NoopExecutor,
)
from doyoutrade.assistant.cron_manager import AgentCronManager
from doyoutrade.persistence.repositories import (
    SqlAlchemyCronJobRepository,
    SqlAlchemyCronJobRunRepository,
)
from doyoutrade.tools._sandbox import knowledge_root

_TERMINAL_STATUSES = {"success", "pre_failed", "agent_failed", "error", "skipped"}


async def _wait_for_terminal(cron_run_repo, run_id, *, timeout_s: float = 15.0) -> dict:
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


async def _stub_statement_provider(account_id, asof: date, captured_at: datetime) -> dict:
    return {
        "asof": asof.isoformat(),
        "source": "broker",
        "account": {
            "source": "broker",
            "captured_at": captured_at.isoformat(),
            "account": {"cash": "12345.67", "equity": "98765.43"},
            "total_market_value": "86419.76",
            "positions": [
                {
                    "symbol": "600000.SH",
                    "name": "PF Bank",
                    "quantity": 1000,
                    "available": 1000,
                    "cost_price": "9.8",
                    "last_price": "10.2",
                    "market_value": "10200",
                    "frozen": 0,
                }
            ],
        },
        "asset": {
            "total_asset": "98765.43",
            "market_value": "86419.76",
            "cash": "12345.67",
            "frozen_cash": "0",
            "available_cash": "12345.67",
            "profit_loss": "400",
            "profit_loss_ratio": 0.004,
        },
        "trades": [
            {
                "trade_id": "tr1",
                "order_id": "o1",
                "symbol": "600000.SH",
                "side": "BUY",
                "quantity": 100,
                "price": "10.2",
                "amount": "1020",
                "trade_time": f"{asof.isoformat()}T10:30:00",
                "commission": "0.5",
            }
        ],
        "trade_count": 1,
        "errors": [],
    }


async def _always_trading(asof: date) -> bool:
    return True


# --- stub market four-dimension providers (keep the e2e hermetic; no akshare
#     network) --------------------------------------------------------------


class _StubBreadth:
    trade_date = "20260617"
    limit_up = list(range(96))
    limit_up_count = 96
    limit_down_count = 12
    broken_board_count = 30
    broken_board_rate = 0.238
    max_streak = 6
    ladder = {"2": 20, "3": 8, "6": 1}
    pool_errors: dict = {}


class _StubBreadthProvider:
    async def fetch_market_breadth(self, trade_date: str):
        return _StubBreadth()


class _StubHeatRow:
    def __init__(self, board_name, change_pct, leader_stock, leader_change_pct):
        self.board_name = board_name
        self.change_pct = change_pct
        self.leader_stock = leader_stock
        self.leader_change_pct = leader_change_pct


class _StubSectorProvider:
    async def get_sector_heat(self, sector_type: str):
        assert sector_type == "concept"
        return [
            _StubHeatRow("AI算力", 4.5, "寒武纪", 20.0),
            _StubHeatRow("固态电池", 2.8, "某龙头", 9.0),
        ]


class _StubLhbRow:
    def __init__(self, symbol, name, reason, change_pct, net_buy_amount):
        self.symbol = symbol
        self.name = name
        self.reason = reason
        self.change_pct = change_pct
        self.net_buy_amount = net_buy_amount


class _StubDragonProvider:
    async def fetch_dragon_tiger(self, start_date: str, end_date: str):
        # 600000.SH is the held name in the stub statement → will be kept.
        return [_StubLhbRow("600000.SH", "PF Bank", "涨幅偏离", 6.0, 5.0e7)]


def _build_cron_manager(ctx, task_registry: JobTaskRegistry):
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
class DailyReviewPipelineE2E(unittest.IsolatedAsyncioTestCase):
    async def test_daily_review_writes_journal_and_pushes(self) -> None:
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            assistant_service = ctx.runtime["assistant_service"]

            agent = await assistant_service.agent_repo.create_agent({
                "name": "E2E Daily Review Agent",
                "system_prompt": "agent for e2e daily_review",
                "status": "active",
            })

            target_session = await assistant_service.create_session(
                agent_id=agent["id"], title="user chat",
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
                DailyReviewExecutor(
                    assistant_service=assistant_service,
                    cron_job_repository=cron_job_repo_for_executor,
                    statement_provider=_stub_statement_provider,
                    trading_day_checker=_always_trading,
                    # Inject stub market providers so the e2e stays hermetic
                    # (no akshare / eastmoney network) while still exercising
                    # the market-four-dimension gather + 情绪周期 log write.
                    market_breadth_provider=_StubBreadthProvider(),
                    sector_provider=_StubSectorProvider(),
                    dragon_tiger_provider=_StubDragonProvider(),
                )
            )

            mgr, _, cron_run_repo = _build_cron_manager(ctx, task_registry)
            await mgr.start()
            try:
                job = await mgr.create_job({
                    "agent_id": agent["id"],
                    "name": "e2e-daily-review",
                    "cron_expression": "30 15 * * mon-fri",
                    "timezone": "Asia/Shanghai",
                    "max_concurrency": 1,
                    "timeout_seconds": 120,
                    "enabled": True,
                    "task_kind": "daily_review",
                    "task_params_json": {
                        "agent_id": agent["id"],
                        "target_session_id": target_session_id,
                        "user_request": "每天收盘后帮我复盘当天交易",
                    },
                })
                run_id = await mgr.trigger_job(job["id"])
                run = await _wait_for_terminal(cron_run_repo, run_id)
            finally:
                await mgr.stop()

            # ── Cron run bookkeeping ─────────────────────────────────────────
            self.assertEqual(run["status"], "success", run)
            self.assertEqual(run["cron_task_kind"], "daily_review", run)
            self.assertEqual(run["delivery_status"], "delivered", run)
            self.assertIsNotNone(run["agent_session_id"], run)
            self.assertIsNone(run["agent_error"], run)
            self.assertNotEqual(run["agent_session_id"], target_session_id)

            # ── Journal persisted to the (isolated) knowledge base ───────────
            journal_root = knowledge_root() / "journal"
            journals = list(journal_root.rglob("*.md")) if journal_root.is_dir() else []
            self.assertTrue(journals, "daily_review did not write any journal markdown")
            body = journals[0].read_text(encoding="utf-8")
            self.assertTrue(body.lstrip().startswith("# "), body[:80])
            # _index.md refreshed after the write
            self.assertTrue((knowledge_root() / "_index.md").exists())

            # ── 情绪周期 log written from the market gather ─────────────────
            sentiment_logs = list(
                (knowledge_root() / "cycles").glob("*/_sentiment.jsonl")
            ) if (knowledge_root() / "cycles").is_dir() else []
            self.assertTrue(
                sentiment_logs, "daily_review did not write a sentiment cycle log"
            )
            import json as _json

            rows = [
                _json.loads(line)
                for line in sentiment_logs[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["limit_up_count"], 96, rows)
            self.assertIn("label", rows[0])

            # ── User-side delivery ───────────────────────────────────────────
            after = await assistant_service.repository.list_messages(
                target_session_id, limit=200, offset=0,
            )
            new_messages = [m for m in after if m["message_id"] not in baseline_ids]
            self.assertEqual(len(new_messages), 1, new_messages)
            pushed = new_messages[0]
            self.assertEqual(pushed["role"], "assistant", pushed)
            meta = pushed.get("metadata") or {}
            self.assertEqual(meta.get("source"), "cron", pushed)
            self.assertEqual(meta.get("cron_task_kind"), "daily_review", pushed)
            self.assertEqual(meta.get("cron_job_run_id"), run_id, pushed)
            self.assertGreater(len((pushed.get("content") or "").strip()), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
