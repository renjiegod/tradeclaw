import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.dml import Update

from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError
from doyoutrade.persistence.models import (
    AgentRecord,
    ApprovalRecord,
    Base,
    CronJobRecord,
    CronJobRunRecord,
    CycleRunRecord,
    DebugSessionEventRecord,
    DebugSessionRecord,
    DebugSessionSpanRecord,
    ModelInvocationRecord,
    Run,
    SystemStateRecord,
    Task,
    TradeFillRecord,
)
from doyoutrade.persistence.repositories import (
    SqlAlchemyAccountRepository,
    SqlAlchemyApprovalRepository,
    SqlAlchemyCycleRunRepository,
    SqlAlchemyDebugSessionRepository,
    SqlAlchemyStrategyDefinitionRepository,
    SqlAlchemyModelRouteRepository,
    SqlAlchemyMonitorAlertRepository,
    SqlAlchemyMonitorRuleRepository,
    SqlAlchemyRunRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTradeFillRepository,
    _is_unique_violation,
)


class _SynchronizedSessionFactory:
    def __init__(
        self,
        base_factory,
        *,
        get_models=(),
        execute_tables=(),
        synchronize_scalar=False,
        synchronize_scalars=False,
        parties=2,
    ):
        self._base_factory = base_factory
        self._get_models = {model for model in get_models}
        self._execute_tables = set(execute_tables)
        self._synchronize_scalar = synchronize_scalar
        self._synchronize_scalars = synchronize_scalars
        self._parties = parties
        self._counts = {}
        self._events = {}
        self._lock = asyncio.Lock()

    def __call__(self):
        return _SynchronizedSessionContext(self._base_factory(), self)

    async def wait(self, key: str):
        async with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            event = self._events.setdefault(key, asyncio.Event())
            if count >= self._parties:
                event.set()
        await asyncio.wait_for(event.wait(), timeout=1)


class _SynchronizedSessionContext:
    def __init__(self, inner_context, coordinator: _SynchronizedSessionFactory):
        self._inner_context = inner_context
        self._coordinator = coordinator

    async def __aenter__(self):
        session = await self._inner_context.__aenter__()
        return _SynchronizedSession(session, self._coordinator)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner_context.__aexit__(exc_type, exc, tb)


class _SynchronizedSession:
    def __init__(self, session, coordinator: _SynchronizedSessionFactory):
        self._session = session
        self._coordinator = coordinator

    async def get(self, model, *args, **kwargs):
        result = await self._session.get(model, *args, **kwargs)
        if model in self._coordinator._get_models:
            await self._coordinator.wait(f"get:{model.__name__}")
        return result

    async def scalar(self, *args, **kwargs):
        result = await self._session.scalar(*args, **kwargs)
        if self._coordinator._synchronize_scalar:
            await self._coordinator.wait("scalar")
        return result

    async def scalars(self, *args, **kwargs):
        result = await self._session.scalars(*args, **kwargs)
        if self._coordinator._synchronize_scalars:
            await self._coordinator.wait("scalars")
        return result

    async def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Update):
            table = getattr(getattr(statement, "table", None), "name", None)
            if table in self._coordinator._execute_tables:
                await self._coordinator.wait(f"execute:{table}")
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


class _FakeOriginalError(Exception):
    def __init__(
        self,
        message,
        *,
        args=None,
        sqlstate=None,
        pgcode=None,
        sqlite_errorname=None,
    ):
        super().__init__(message)
        self.args = args or (message,)
        self.sqlstate = sqlstate
        self.pgcode = pgcode
        self.sqlite_errorname = sqlite_errorname


def _make_integrity_error(original) -> IntegrityError:
    return IntegrityError("statement", {}, original)


class _FailingCommitSession:
    def add(self, _record):
        pass

    async def commit(self):
        raise _make_integrity_error(
            _FakeOriginalError(
                "CHECK constraint failed: tasks.status",
                sqlite_errorname="SQLITE_CONSTRAINT_CHECK",
            )
        )

    async def rollback(self):
        pass


class _FailingCommitSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return _FailingCommitSession()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class PersistenceErrorClassificationTests(unittest.TestCase):
    def test_unique_violation_detection_recognizes_sqlite_postgres_and_mysql_duplicates(self):
        sqlite_error = _make_integrity_error(
            _FakeOriginalError(
                "UNIQUE constraint failed: approvals.approval_id",
                sqlite_errorname="SQLITE_CONSTRAINT_UNIQUE",
            )
        )
        postgres_error = _make_integrity_error(
            _FakeOriginalError(
                'duplicate key value violates unique constraint "approvals_pkey"',
                sqlstate="23505",
            )
        )
        mysql_error = _make_integrity_error(
            _FakeOriginalError(
                "Duplicate entry 'approval-1' for key 'PRIMARY'",
                args=(1062, "Duplicate entry 'approval-1' for key 'PRIMARY'"),
            )
        )

        self.assertTrue(_is_unique_violation(sqlite_error))
        self.assertTrue(_is_unique_violation(postgres_error))
        self.assertTrue(_is_unique_violation(mysql_error))

    def test_unique_violation_detection_rejects_non_unique_integrity_errors(self):
        error = _make_integrity_error(
            _FakeOriginalError(
                "NOT NULL constraint failed: approvals.mode",
                sqlite_errorname="SQLITE_CONSTRAINT_NOTNULL",
            )
        )

        self.assertFalse(_is_unique_violation(error))


class PersistenceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_approval_repo_list_approvals_filters_and_pagination(self):
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        base = datetime(2026, 6, 1, 0, 0, 0)

        async def _seed(approval_id, intent_id, symbol, hours):
            return await repo.create_pending(
                approval_id=approval_id,
                intent_id=intent_id,
                mode="live",
                created_at=base + timedelta(hours=hours),
                expires_at=base + timedelta(hours=hours + 1),
                symbol=symbol,
                action="buy",
                account_id="acct-1",
                notional="1000",
            )

        await _seed("appr-a", "intent-a", "600000.SH", 1)  # stays pending
        await _seed("appr-b", "intent-b", "600519.SH", 2)
        await repo.resolve("appr-b", "approved", resolver_id="u1", decision_source="web")
        await _seed("appr-c", "intent-c", "600000.SH", 3)
        await repo.resolve("appr-c", "rejected", reason="no", decision_source="feishu_card")

        # No filter → all, newest-first, with full total.
        items, total = await repo.list_approvals()
        self.assertEqual(total, 3)
        self.assertEqual([i.approval_id for i in items], ["appr-c", "appr-b", "appr-a"])

        # Status filter.
        pend, total = await repo.list_approvals(statuses=["pending"])
        self.assertEqual((total, [i.approval_id for i in pend]), (1, ["appr-a"]))

        # Symbol filter.
        by_sym, total = await repo.list_approvals(symbol="600000.SH")
        self.assertEqual(total, 2)
        self.assertEqual({i.approval_id for i in by_sym}, {"appr-a", "appr-c"})

        # Case-insensitive substring search (ilike) over intent_id.
        found, total = await repo.list_approvals(search="INTENT-B")
        self.assertEqual((total, [i.approval_id for i in found]), (1, ["appr-b"]))

        # created_after keeps only the latest row.
        after, total = await repo.list_approvals(created_after=base + timedelta(hours=2, minutes=30))
        self.assertEqual((total, [i.approval_id for i in after]), (1, ["appr-c"]))

        # Pagination returns a page but the FULL match total.
        page1, total = await repo.list_approvals(limit=2, offset=0)
        self.assertEqual((total, [i.approval_id for i in page1]), (3, ["appr-c", "appr-b"]))
        page2, total = await repo.list_approvals(limit=2, offset=2)
        self.assertEqual((total, [i.approval_id for i in page2]), (3, ["appr-a"]))

    async def test_approval_repo_list_approvals_run_id_filter(self):
        # The cycle-detail view (周期详情) lists approvals tied to ONE run_id.
        # ApprovalRecord.run_id is indexed; the filter must scope to it exactly.
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        base = datetime(2026, 6, 2, 0, 0, 0)

        async def _seed(approval_id, intent_id, run_id, hours):
            return await repo.create_pending(
                approval_id=approval_id,
                intent_id=intent_id,
                mode="live",
                created_at=base + timedelta(hours=hours),
                expires_at=base + timedelta(hours=hours + 1),
                run_id=run_id,
                task_id="task-1",
                symbol="600000.SH",
                action="buy",
                account_id="acct-1",
                notional="1000",
            )

        await _seed("appr-r1a", "intent-r1a", "run-1", 1)
        await _seed("appr-r1b", "intent-r1b", "run-1", 2)
        await _seed("appr-r2", "intent-r2", "run-2", 3)

        r1, total = await repo.list_approvals(run_id="run-1")
        self.assertEqual(total, 2)
        self.assertEqual({i.approval_id for i in r1}, {"appr-r1a", "appr-r1b"})
        # run_id combines with other filters (AND semantics).
        r2, total = await repo.list_approvals(run_id="run-2", symbol="600000.SH")
        self.assertEqual((total, [i.approval_id for i in r2]), (1, ["appr-r2"]))
        # A run with no approvals returns an explicit empty, not all rows.
        none_rows, total = await repo.list_approvals(run_id="run-missing")
        self.assertEqual((total, none_rows), (0, []))

    async def test_trade_fill_get_by_intent_id(self):
        # Approval receipts join the eventual fill by intent_id (it lands in a
        # LATER resume cycle, not the approval's originating run_id).
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        await repo.insert_fill(
            task_id="task-1",
            cycle_run_id="run-resume-1",
            run_id=None,
            symbol="600000.SH",
            side="buy",
            quantity="200",
            price="7.80",
            amount="1560",
            fee=None,
            currency=None,
            intent_id="intent-appr-1",
            rationale="approved order",
            filled_at=datetime(2026, 6, 14, 1, 1, 0),
            source_mode="paper",
        )

        hit = await repo.get_by_intent_id(task_id="task-1", intent_id="intent-appr-1")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["quantity"], "200")
        self.assertEqual(hit["price"], "7.80")
        self.assertEqual(hit["cycle_run_id"], "run-resume-1")
        # Unknown intent / blank → None (no silent wrong match).
        self.assertIsNone(await repo.get_by_intent_id(task_id="task-1", intent_id="nope"))
        self.assertIsNone(await repo.get_by_intent_id(task_id="task-1", intent_id=""))

    async def test_account_repo_crud_and_session_id_update(self):
        repo = SqlAlchemyAccountRepository(self.session_factory)
        self.assertEqual(await repo.list_accounts(), [])
        created = await repo.upsert_account(
            {"name": "live-a", "mode": "live", "base_url": "http://x:9",
             "qmt_account_id": "10000001", "qmt_terminal_id": "dgzq", "token": "tok"}
        )
        self.assertTrue(created["id"].startswith("acct-"))
        self.assertEqual(created["mode"], "live")
        self.assertFalse(created["is_default"])
        # multi-terminal routing field round-trips through serializer + whitelist
        self.assertEqual(created["qmt_terminal_id"], "dgzq")
        got = await repo.get_account(created["id"])
        self.assertEqual(got["qmt_account_id"], "10000001")
        self.assertEqual(got["qmt_terminal_id"], "dgzq")
        # update via upsert (id present)
        updated = await repo.upsert_account({"id": created["id"], "name": "renamed"})
        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(updated["token"], "tok")  # untouched fields preserved
        # session_id write-back
        await repo.update_session_id(created["id"], "sess-xyz")
        self.assertEqual((await repo.get_account(created["id"]))["session_id"], "sess-xyz")
        # delete
        await repo.delete_account(created["id"])
        self.assertIsNone(await repo.get_account(created["id"]))

    async def test_account_repo_set_default_is_exclusive(self):
        repo = SqlAlchemyAccountRepository(self.session_factory)
        a = await repo.upsert_account({"name": "a", "mode": "mock", "base_url": "http://x:9"})
        b = await repo.upsert_account({"name": "b", "mode": "live", "base_url": "http://y:9"})
        await repo.set_default(a["id"])
        self.assertEqual((await repo.get_default_account())["id"], a["id"])
        # switching default clears the prior one (at-most-one invariant)
        await repo.set_default(b["id"])
        default = await repo.get_default_account()
        self.assertEqual(default["id"], b["id"])
        self.assertFalse((await repo.get_account(a["id"]))["is_default"])

    async def test_account_repo_default_skips_disabled(self):
        repo = SqlAlchemyAccountRepository(self.session_factory)
        a = await repo.upsert_account({"name": "a", "mode": "mock", "base_url": "http://x:9"})
        await repo.set_default(a["id"])
        await repo.upsert_account({"id": a["id"], "enabled": False})
        # a disabled default is not returned as the usable default
        self.assertIsNone(await repo.get_default_account())

    async def test_monitor_rule_and_alert_round_trip(self):
        from datetime import datetime, timezone

        rules = SqlAlchemyMonitorRuleRepository(self.session_factory)
        alerts = SqlAlchemyMonitorAlertRepository(self.session_factory)

        rule = await rules.create_rule(
            name="涨停盯盘",
            enabled=True,
            status="active",
            scope_kind="watchlist_tag",
            scope_json={"tag": "半导体"},
            condition_json={"op": "and", "children": [{"preset": "limit_up"}]},
            delivery_json={"mode": "card", "target": {"kind": "channel", "channel_id": "chan-1", "chat_id": "oc_1"}},
            cooldown_seconds=300,
        )
        self.assertTrue(rule.id.startswith("mon-"))
        self.assertEqual(rule.scope_json, {"tag": "半导体"})

        got = await rules.get_rule(rule.id)
        self.assertEqual(got.condition_json["op"], "and")
        self.assertEqual([r.id for r in await rules.list_active()], [rule.id])

        # patch semantics + status flip removes it from the active set
        await rules.update_rule(rule.id, enabled=False, status="paused")
        self.assertEqual(await rules.list_active(), [])

        # malformed JSON column raises (not silently coerced)
        with self.assertRaises(ValueError):
            await rules.create_rule(
                name="bad", enabled=True, status="active", scope_kind="symbols",
                scope_json=["not", "a", "dict"], condition_json={"preset": "limit_up"},
                delivery_json=None, cooldown_seconds=0,
            )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        a = await alerts.insert_alert(
            monitor_rule_id=rule.id, symbol="000001.SZ", condition_name="limit_up",
            transition_key="2026-06-22", triggered_at=now, last_price=11.0, limit_price=11.0,
            diagnostics_json={"leaves": []}, run_id="run-abc", delivery_status="pending",
        )
        self.assertEqual(a.symbol, "000001.SZ")
        self.assertEqual(a.run_id, "run-abc")
        dedup = await alerts.recent_for_dedup(rule.id, "000001.SZ", "limit_up")
        self.assertIsNotNone(dedup)
        self.assertEqual(dedup.id, a.id)
        latest = await alerts.list_latest_per_dedup_key()
        self.assertEqual(len(latest), 1)
        await alerts.mark_delivered(a.id, delivery_status="forwarded", delivered_at=now)
        self.assertEqual((await alerts.list_for_rule(rule.id))[0].delivery_status, "forwarded")

        # rule delete (the FK ondelete=CASCADE that purges alerts is enforced by
        # Postgres in prod; SQLite needs PRAGMA foreign_keys and is not asserted here)
        await rules.delete_rule(rule.id)
        with self.assertRaises(RecordNotFoundError):
            await rules.get_rule(rule.id)

    def test_persistence_metadata_exposes_expected_runtime_tables(self):
        self.assertEqual(
            set(Base.metadata.tables),
            {
                "tasks",
                "task_triggers",
                "approvals",
                "system_state",
                "model_invocations",
                "debug_sessions",
                "debug_session_events",
                "debug_session_spans",
                "cycle_runs",
                "runs",
                "trade_fills",
                "instrument_catalog",
                "model_routes",
                "strategy_definitions",
                "assistant_sessions",
                "assistant_messages",
                "assistant_events",
                "assistant_loaded_skills",
                "assistant_job_watches",
                "channel_peer_sessions",
                "agents",
                "channels",
                "cron_jobs",
                "cron_job_runs",
                "cached_bars",
                "cached_bar_ranges",
                "cached_bar_suspensions",
                "market_bars",
                "market_bar_sync_state",
                "accounts",
                "watchlist_entries",
                "monitor_rules",
                "monitor_alerts",
                # 功能 5: decision signal lifecycle + backtest verification.
                "decision_signals",
                "decision_signal_outcomes",
                # Knowledge graph: base projection (nodes/edges/source_state)
                # plus the audited manual-edit / agent-proposal-approval schema
                # (change sets + operations, revisions, evidence, entity
                # lineage, conflicts, canvas layouts, custom schema items,
                # one-time approval decisions, and the locked graph_state row).
                "kg_nodes",
                "kg_edges",
                "kg_source_state",
                "kg_graph_state",
                "kg_change_sets",
                "kg_change_operations",
                "kg_revisions",
                "kg_evidence",
                "kg_entity_lineage",
                "kg_conflicts",
                "kg_canvas_layouts",
                "kg_schema_items",
                "kg_approval_decisions",
                # Pre-existing swarm orchestration tables (already in the schema
                # via the swarm migration; the expected set had not been synced).
                "swarm_runs",
                "swarm_tasks",
                "swarm_events",
            },
        )

    def test_alembic_upgrade_creates_runtime_schema(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "test-persistence-alembic.db"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-x",
                    f"db_url=sqlite:///{db_path}",
                    "upgrade",
                    "head",
                ],
                cwd=project_root,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}",
            )

            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                task_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('tasks')")
                }
                approval_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('approvals')")
                }
                system_state_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('system_state')")
                }
                channel_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info('channels')")
                }
                agent_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info('agents')")
                }
                channel_peer_session_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info('channel_peer_sessions')")
                }

            self.assertTrue(
                {
                    "tasks",
                    "approvals",
                    "system_state",
                    "model_invocations",
                    "debug_sessions",
                    "debug_session_events",
                    "strategy_definitions",
                    "channels",
                    "channel_peer_sessions",
                }.issubset(tables)
            )
            self.assertNotIn("market_bars", tables)
            self.assertNotIn("market_bar_sync_state", tables)
            self.assertEqual(
                channel_peer_session_columns,
                {"channel_id", "peer_session_id", "active_session_id", "updated_at"},
            )
            self.assertTrue(
                {
                    "id",
                    "name",
                    "type",
                    "enabled",
                    "agent_id",
                    "status",
                    "last_error",
                    "last_connected_at",
                    "config",
                    "secrets",
                    "created_at",
                    "updated_at",
                }.issubset(channel_columns)
            )
            self.assertIn("system_prompt_template_id", agent_columns)
            self.assertTrue(
                {
                    "ix_approvals_status",
                    "ix_approvals_expires_at",
                    "ix_approvals_status_expires_at",
                    "ix_model_invocations_created_at",
                    "ix_model_invocations_trace_id",
                    "ix_model_invocations_span_id",
                    "ix_model_invocations_run_id",
                    "ix_debug_sessions_task_created_at",
                    "ix_cycle_runs_task_started",
                    "ix_trade_fills_task_run_symbol_time",
                    "ix_channels_type_enabled",
                    "ix_channels_agent_id",
                }.issubset(indexes)
            )
            self.assertIn(task_defaults["description"], (None, "''"))
            self.assertIn(task_defaults["last_error"], (None, "''"))
            self.assertIn("universe", task_defaults)
            self.assertIn(task_defaults["universe"], (None, "'[]'", "[]"))
            self.assertEqual(approval_defaults["reason"], "''")
            self.assertEqual(system_state_defaults["kill_switch_enabled"], "0")

    async def test_db_factory_returns_real_async_sqlalchemy_primitives(self):
        self.assertIsInstance(self.engine, AsyncEngine)

        async with self.session_factory() as session:
            self.assertIsInstance(session, AsyncSession)

    async def test_channel_peer_session_round_trip_and_upsert(self):
        from doyoutrade.assistant.repository import SqlAlchemyAssistantRepository

        repo = SqlAlchemyAssistantRepository(self.session_factory)
        # Unknown peer resolves to None (caller falls back to the deterministic peer session).
        self.assertIsNone(await repo.get_active_peer_session("feishu", "channel:feishu:userA"))
        # First /new rebinding persists.
        await repo.set_active_peer_session("feishu", "channel:feishu:userA", "asst-111")
        self.assertEqual(
            await repo.get_active_peer_session("feishu", "channel:feishu:userA"), "asst-111"
        )
        # A later /new for the same peer upserts (no duplicate-PK error, latest wins).
        await repo.set_active_peer_session("feishu", "channel:feishu:userA", "asst-222")
        self.assertEqual(
            await repo.get_active_peer_session("feishu", "channel:feishu:userA"), "asst-222"
        )
        # A different peer is independent.
        self.assertIsNone(await repo.get_active_peer_session("feishu", "channel:feishu:userB"))

    async def test_assistant_events_tail_returns_most_recent_not_oldest(self):
        """SQL-backed twin of the in-memory-repo test in test_assistant_service.py:
        `tail=True` must page from the end (ORDER BY id DESC + reverse), not
        silently hand back the session's earliest events via a plain
        ascending `.limit()` — the bug that made a long assistant session's
        live "current attempt" reconstruction see stale, already-finished
        tool calls instead of the in-flight ones."""
        from doyoutrade.assistant.repository import SqlAlchemyAssistantRepository

        repo = SqlAlchemyAssistantRepository(self.session_factory)
        session = await repo.create_session(agent_id="test-agent", title="tail-sql")
        session_id = session["session_id"]
        for i in range(10):
            await repo.append_event(
                session_id=session_id,
                event_type="thinking.delta",
                payload={"attempt_id": "attempt-old", "delta": str(i)},
            )

        default_page = await repo.list_events(session_id, after_id=None, limit=5)
        self.assertEqual([e["payload"]["delta"] for e in default_page], ["0", "1", "2", "3", "4"])

        tail_page = await repo.list_events(session_id, after_id=None, limit=5, tail=True)
        self.assertEqual([e["payload"]["delta"] for e in tail_page], ["5", "6", "7", "8", "9"])

        # A forward-paginated read (after_id set) ignores `tail` and just
        # continues chronologically from the marker.
        forward = await repo.list_events(
            session_id, after_id=default_page[-1]["event_id"], limit=3, tail=True
        )
        self.assertEqual([e["payload"]["delta"] for e in forward], ["5", "6", "7"])

    async def test_trade_fill_repository_insert_and_list_for_run(self):
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        inserted = await repo.insert_fill(
            task_id="task-1",
            cycle_run_id="run-cycle-1",
            run_id="run-parent-1",
            session_id="session-1",
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.25",
            amount="1025",
            fee=None,
            currency=None,
            intent_id="intent-1",
            rationale="factor.macd.golden_cross",
            filled_at=datetime(2026, 4, 26, 12, 0, 0),
            source_mode="backtest",
            raw_payload={"x": 1},
        )
        self.assertTrue(inserted)

        rows = await repo.list_for_task_run(task_id="task-1", run_id="run-parent-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "600000.SH")
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["price"], "10.25")
        self.assertEqual(rows[0]["quantity"], "100")
        self.assertEqual(rows[0]["rationale"], "factor.macd.golden_cross")
        # Buy fills carry no exit_reason.
        self.assertIsNone(rows[0]["exit_reason"])

    async def test_trade_fill_repository_list_for_task_filters_by_source_mode(self):
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        self.assertTrue(
            await repo.insert_fill(
                task_id="task-mode",
                cycle_run_id="run-cycle-live",
                run_id="run-parent-live",
                symbol="600000.SH",
                side="buy",
                quantity="100",
                price="10.00",
                filled_at=datetime(2026, 4, 26, 12, 0, 0),
                source_mode="live",
            )
        )
        self.assertTrue(
            await repo.insert_fill(
                task_id="task-mode",
                cycle_run_id="run-cycle-paper",
                run_id="run-parent-paper",
                symbol="600519.SH",
                side="buy",
                quantity="200",
                price="20.00",
                filled_at=datetime(2026, 4, 27, 12, 0, 0),
                source_mode="paper",
            )
        )
        live_rows = await repo.list_for_task(task_id="task-mode", source_mode="live")
        self.assertEqual(len(live_rows), 1)
        self.assertEqual(live_rows[0]["symbol"], "600000.SH")
        all_rows = await repo.list_for_task(task_id="task-mode")
        self.assertEqual(len(all_rows), 2)

    async def test_trade_fill_repository_persists_exit_reason(self):
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        self.assertTrue(
            await repo.insert_fill(
                task_id="task-er",
                cycle_run_id="run-cycle-er",
                run_id="run-parent-er",
                symbol="600000.SH",
                side="sell",
                quantity="100",
                price="11.00",
                filled_at=datetime(2026, 4, 27, 12, 0, 0),
                source_mode="backtest",
                exit_tag="ma_cross",
                exit_reason="take_profit",
            )
        )
        rows = await repo.list_for_task_run(task_id="task-er", run_id="run-parent-er")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "take_profit")
        self.assertEqual(rows[0]["exit_tag"], "ma_cross")

    async def test_trade_fill_repository_dedupes_same_fill(self):
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        payload = dict(
            task_id="task-1",
            cycle_run_id="run-cycle-1",
            run_id="run-parent-1",
            session_id="session-1",
            symbol="600000.SH",
            side="buy",
            quantity="100",
            price="10.25",
            amount="1025",
            fee=None,
            currency=None,
            intent_id="intent-1",
            rationale="factor.macd.golden_cross",
            filled_at=datetime(2026, 4, 26, 12, 0, 0),
            source_mode="backtest",
            raw_payload=None,
        )
        self.assertTrue(await repo.insert_fill(**payload))
        self.assertFalse(await repo.insert_fill(**payload))

        async with self.session_factory() as session:
            n = await session.scalar(select(func.count()).select_from(TradeFillRecord))
        self.assertEqual(int(n or 0), 1)

    async def test_instance_repository_persists_status_transitions(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        created = await repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        updated = await repo.update_status("instance-1", "running", "")

        self.assertEqual(created.task_id, "instance-1")
        self.assertEqual(updated.status, "running")

    async def test_duplicate_instance_create_raises_repository_conflict(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        await repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        with self.assertRaises(StateConflictError):
            await repo.create_task(
                task_id="instance-1",
                name="alpha-duplicate",
                mode="paper",
                orchestrator_mode="single-agent",
                description="demo",
                data_provider="mock",
                status="configured",
                last_error="",
            )

    async def test_instance_repository_allows_duplicate_names(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        first = await repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            orchestrator_mode="single-agent",
            description="first",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        second = await repo.create_task(
            task_id="instance-2",
            name="alpha",
            mode="backtest",
            orchestrator_mode="single-agent",
            description="second",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        listed = await repo.list_tasks()

        self.assertEqual(first.name, "alpha")
        self.assertEqual(second.name, "alpha")
        self.assertEqual(first.task_id, "instance-1")
        self.assertEqual(second.task_id, "instance-2")
        self.assertEqual([item.task_id for item in listed], ["instance-2", "instance-1"])

    async def test_instance_repository_non_unique_integrity_error_includes_details(self):
        repo = SqlAlchemyTaskRepository(_FailingCommitSessionFactory())

        with self.assertRaises(PersistenceError) as ctx:
            await repo.create_task(
                task_id="instance-1",
                name="alpha",
                mode="paper",
                description="demo",
                data_provider="mock",
                status="configured",
                last_error="",
            )

        self.assertEqual(
            str(ctx.exception),
            "failed to create task: check constraint failed: tasks.status",
        )

    async def test_instance_repository_lists_and_resolves_instances(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        first = await repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            orchestrator_mode="single-agent",
            description="first",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        second = await repo.create_task(
            task_id="instance-2",
            name="beta",
            mode="paper",
            orchestrator_mode="single-agent",
            description="second",
            data_provider=None,
            status="paused",
            last_error="",
        )

        listed = await repo.list_tasks()
        by_id = await repo.get_task(first.task_id)
        by_second_id = await repo.get_task(second.task_id)

        self.assertEqual([item.task_id for item in listed], ["instance-2", "instance-1"])
        self.assertEqual(by_id.name, "alpha")
        self.assertEqual(by_second_id.task_id, "instance-2")

    async def test_instance_repository_list_page_supports_filters_and_pagination(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        await repo.create_task(
            task_id="task-a",
            name="alpha-backtest",
            mode="backtest",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="completed",
            last_error="",
        )
        await repo.create_task(
            task_id="task-b",
            name="beta-paper",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="running",
            last_error="",
        )
        await repo.create_task(
            task_id="task-c",
            name="alpha-paper",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="running",
            last_error="",
        )

        page, total = await repo.list_tasks_page(
            q="alpha",
            status="running",
            mode="paper",
            definition_id=None,
            limit=1,
            offset=0,
        )

        self.assertEqual(total, 1)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0].task_id, "task-c")

    async def test_instance_repository_list_page_filters_by_modes_set(self):
        # The UI groups its tabs with a ``modes`` set ("trading" = paper/live/
        # signal_only vs "backtest"); it must take precedence over the single
        # ``mode`` and exclude every mode not in the set.
        repo = SqlAlchemyTaskRepository(self.session_factory)
        for task_id, name, mode in (
            ("task-bt", "alpha-backtest", "backtest"),
            ("task-paper", "beta-paper", "paper"),
            ("task-live", "gamma-live", "live"),
            ("task-signal", "delta-signal", "signal_only"),
        ):
            await repo.create_task(
                task_id=task_id,
                name=name,
                mode=mode,
                orchestrator_mode="single-agent",
                description="",
                data_provider="mock",
                status="configured",
                last_error="",
            )

        trading, trading_total = await repo.list_tasks_page(
            q=None,
            status=None,
            mode=None,
            modes=["paper", "live", "signal_only"],
            definition_id=None,
            limit=10,
            offset=0,
        )
        self.assertEqual(trading_total, 3)
        self.assertEqual(
            {row.task_id for row in trading},
            {"task-paper", "task-live", "task-signal"},
        )

        backtest, backtest_total = await repo.list_tasks_page(
            q=None,
            status=None,
            mode=None,
            modes=["backtest"],
            definition_id=None,
            limit=10,
            offset=0,
        )
        self.assertEqual(backtest_total, 1)
        self.assertEqual([row.task_id for row in backtest], ["task-bt"])

        # ``modes`` wins over a conflicting single ``mode``.
        overridden, overridden_total = await repo.list_tasks_page(
            q=None,
            status=None,
            mode="backtest",
            modes=["paper"],
            definition_id=None,
            limit=10,
            offset=0,
        )
        self.assertEqual(overridden_total, 1)
        self.assertEqual([row.task_id for row in overridden], ["task-paper"])

    async def test_instance_repository_list_page_filters_by_strategy_definition_id(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        await repo.create_task(
            task_id="task-sd-a",
            name="alpha-sd",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="running",
            last_error="",
            settings={"strategy_definition_id": "sd-alpha"},
        )
        await repo.create_task(
            task_id="task-sd-b",
            name="beta-sd",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="running",
            last_error="",
            settings={"strategy": {"definition_id": "sd-beta"}},
        )

        page, total = await repo.list_tasks_page(
            q=None,
            status=None,
            mode=None,
            definition_id="sd-alpha",
            limit=10,
            offset=0,
        )

        self.assertEqual(total, 1)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0].task_id, "task-sd-a")

    async def test_instance_repository_update_task_syncs_strategy_definition_id_column(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        await repo.create_task(
            task_id="task-sync",
            name="sync",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="configured",
            last_error="",
            settings={"strategy": {"definition_id": "sd-old"}},
        )
        updated = await repo.update_task(
            "task-sync",
            settings={"strategy": {"definition_id": "sd-new"}},
        )
        self.assertEqual(
            (updated.settings or {}).get("strategy", {}).get("definition_id"),
            "sd-new",
        )

        page, total = await repo.list_tasks_page(
            q=None,
            status=None,
            mode=None,
            definition_id="sd-new",
            limit=10,
            offset=0,
        )
        self.assertEqual(total, 1)
        self.assertEqual(page[0].task_id, "task-sync")

    async def test_instance_repository_raises_for_missing_instance(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        with self.assertRaises(RecordNotFoundError):
            await repo.update_status("missing", "running", "")

        with self.assertRaises(RecordNotFoundError):
            await repo.get_task("missing")

    async def test_task_repository_persists_backtest_summary_round_trip(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        await repo.create_task(
            task_id="task-bs-1",
            name="task-bs-1",
            mode="backtest",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        summary = {
            "schema_version": 1,
            "run_id": "run_x",
            "completed_at": "2026-01-31T07:00:00Z",
            "starting_equity": "100000",
            "ending_equity": "110000",
            "return_pct": "10",
            "trade_count_closed": 4,
            "win_rate": "0.5",
            "equity_curve": [
                {"t": "2026-01-01T07:00:00Z", "equity": "100000"},
                {"t": "2026-01-31T07:00:00Z", "equity": "110000"},
            ],
            "equity_curve_meta": {"downsampled": False, "raw_length": 2},
        }
        updated = await repo.update_backtest_summary_and_status(
            "task-bs-1", summary=summary, status="completed"
        )
        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.backtest_summary, summary)

        fetched = await repo.get_task("task-bs-1")
        self.assertEqual(fetched.status, "completed")
        self.assertEqual(fetched.backtest_summary, summary)

    async def test_task_repository_update_backtest_summary_rejects_non_backtest_task(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        await repo.create_task(
            task_id="task-paper-1",
            name="task-paper-1",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        with self.assertRaises(ValueError):
            await repo.update_backtest_summary_and_status(
                "task-paper-1",
                summary={"schema_version": 1, "run_id": "x"},
                status="completed",
            )

    async def test_task_repository_update_backtest_summary_missing_task_raises_not_found(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)
        with self.assertRaises(RecordNotFoundError):
            await repo.update_backtest_summary_and_status(
                "missing-bt", summary={"schema_version": 1}, status="error"
            )

    async def test_instance_repository_delete_removes_row(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        created = await repo.create_task(
            task_id="instance-del-1",
            name="to-delete",
            mode="paper",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        await repo.delete_task(created.task_id)

        listed = await repo.list_tasks()
        self.assertEqual(listed, [])

        with self.assertRaises(RecordNotFoundError):
            await repo.get_task(created.task_id)

    async def test_instance_repository_delete_cascades_related_runtime_records(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        created = await repo.create_task(
            task_id="instance-del-linked",
            name="to-delete-linked",
            mode="backtest",
            orchestrator_mode="single-agent",
            description="",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        async with self.session_factory() as session:
            session.add(
                Run(
                    run_id="btjob-del-linked",
                    task_id=created.task_id,
                    mode="backtest",
                    status="completed",
                    market_profile="cn_a_share",
                    bar_interval="1d",
                    range_start_utc=datetime(2026, 1, 1, 0, 0, 0),
                    range_end_utc=datetime(2026, 1, 2, 0, 0, 0),
                    session_id="session-del-linked",
                    error_message="",
                    bars_total=1,
                    bars_completed=1,
                )
            )
            session.add(
                DebugSessionRecord(
                    session_id="session-del-linked",
                    task_id=created.task_id,
                    status="completed",
                    run_id="btjob-del-linked",
                    error_message="",
                    config_overrides=None,
                    input_overrides=None,
                    effective_config=None,
                    session_type="backtest",
                )
            )
            session.add(
                DebugSessionEventRecord(
                    session_id="session-del-linked",
                    sequence=1,
                    event_type="phase",
                    payload={"phase": "x"},
                )
            )
            session.add(
                DebugSessionSpanRecord(
                    span_id="span-del-linked",
                    trace_id="trace-del-linked",
                    parent_span_id=None,
                    session_id="session-del-linked",
                    name="run_cycle",
                    span_type="worker",
                    start_time=datetime(2026, 1, 1, 0, 0, 0),
                    end_time=datetime(2026, 1, 1, 0, 0, 1),
                    duration_ms=1000.0,
                    attributes={},
                    status="ok",
                    span_source="backtest",
                )
            )
            session.add(
                CycleRunRecord(
                    run_id="cycle-del-linked",
                    task_id=created.task_id,
                    agent_name="ag",
                    session_id="session-del-linked",
                    trace_id="trace-del-linked",
                    run_mode="backtest",
                    run_kind="backtest_bar",
                    clock_mode="simulated",
                    wall_started_at=datetime(2026, 1, 1, 0, 0, 0),
                    status="completed",
                )
            )
            session.add(
                ModelInvocationRecord(
                    model_id="anthropic-model",
                    provider_kind="anthropic",
                    model_route_name=None,
                    provider_key=None,
                    model="test-model",
                    task_id=created.task_id,
                    run_id="cycle-del-linked",
                    trace_id="trace-del-linked",
                    span_id="span-del-linked",
                    call_kind="signal",
                    first_token_latency_ms=None,
                    total_latency_ms=1,
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    ok=True,
                    error_message="",
                    request_payload={},
                    response_payload={},
                )
            )
            await session.commit()

        await repo.delete_task(created.task_id)

        async with self.session_factory() as session:
            self.assertIsNone(await session.get(Task, created.task_id))
            self.assertIsNone(await session.get(Run, "btjob-del-linked"))
            self.assertIsNone(await session.get(CycleRunRecord, "cycle-del-linked"))
            self.assertIsNone(await session.get(DebugSessionRecord, "session-del-linked"))
            self.assertIsNone(await session.get(DebugSessionSpanRecord, "span-del-linked"))
            self.assertEqual(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(DebugSessionEventRecord)
                        .where(DebugSessionEventRecord.session_id == "session-del-linked")
                    )
                    or 0
                ),
                0,
            )
            self.assertEqual(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ModelInvocationRecord)
                        .where(ModelInvocationRecord.task_id == created.task_id)
                    )
                    or 0
                ),
                0,
            )

    async def test_instance_repository_delete_raises_for_missing(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        with self.assertRaises(RecordNotFoundError):
            await repo.delete_task("missing-id")

    async def test_strategy_definition_repository_supports_updates(self):
        self.strategy_definition_repo = SqlAlchemyStrategyDefinitionRepository(self.session_factory)

        definition = await self.strategy_definition_repo.create_definition(
            definition_id="def-update-1",
            name="Before",
            current_version=None,
            api_version="v1",
            input_contract_json={"kind": "bars"},
            parameter_schema_json={},
            default_parameters_json={"threshold": 0.2},
            capabilities_json={"supports_live": False},
            provenance_json={"origin": "test"},
            code_hash="hash-before",
            generation_prompt="before",
            generation_model="gpt-test",
            generation_metadata_json={},
            status="active",
        )
        self.assertIsNone(definition.current_version)

        updated_definition = await self.strategy_definition_repo.update_definition(
            "def-update-1",
            name="After",
            current_version="v0001-abc123",
            parameter_schema_json={"threshold": {"type": "number"}},
            code_hash="hash-after",
        )

        self.assertEqual(updated_definition.name, "After")
        self.assertEqual(updated_definition.code_hash, "hash-after")
        self.assertEqual(updated_definition.current_version, "v0001-abc123")

    async def test_delete_definition_removes_row(self):
        self.strategy_definition_repo = SqlAlchemyStrategyDefinitionRepository(self.session_factory)

        definition = await self.strategy_definition_repo.create_definition(
            definition_id="def-delete-1",
            name="Delete Definition",
            current_version=None,
            api_version="v1",
            input_contract_json={},
            parameter_schema_json={},
            default_parameters_json={},
            capabilities_json={},
            provenance_json={"origin": "test"},
            code_hash="hash-delete",
            generation_prompt="delete",
            generation_model="gpt-test",
            generation_metadata_json={},
            status="active",
        )

        await self.strategy_definition_repo.delete_definition(definition.definition_id)

        with self.assertRaises(RecordNotFoundError):
            await self.strategy_definition_repo.get_definition(definition.definition_id)

    async def test_read_current_code_raises_version_not_found_when_no_version(self):
        """read_current_code raises VersionNotFound when current_version is NULL."""
        from doyoutrade.persistence.strategy_storage import StrategyStorage, VersionNotFound

        repo = SqlAlchemyStrategyDefinitionRepository(
            self.session_factory,
            storage=StrategyStorage(Path(self.tempdir.name) / "strategy_storage"),
        )
        await repo.create_definition(
            definition_id="def-no-version",
            name="No Version Yet",
            current_version=None,
            api_version="v1",
            input_contract_json=None,
            parameter_schema_json=None,
            default_parameters_json=None,
            capabilities_json=None,
            provenance_json=None,
            code_hash="hash-none",
            generation_prompt="",
            generation_model="",
            generation_metadata_json=None,
            status="active",
        )
        with self.assertRaises(VersionNotFound):
            await repo.read_current_code("def-no-version")

    async def test_model_route_repository_round_trip(self):
        route_repo = SqlAlchemyModelRouteRepository(self.session_factory)

        route = await route_repo.create(
            route_name="signals-fast",
            provider_kind="anthropic",
            api_key="secret",
            target_model="claude-3",
            settings={"temperature": 0.2, "max_tokens": 1024},
        )

        by_name = await route_repo.get_by_route_name("signals-fast")
        self.assertEqual(by_name.id, route.id)
        self.assertEqual(by_name.provider_kind, "anthropic")
        self.assertEqual(by_name.api_key, "secret")
        self.assertEqual(by_name.target_model, "claude-3")
        self.assertEqual(by_name.settings, {"temperature": 0.2, "max_tokens": 1024})

        by_id = await route_repo.get_by_id(route.id)
        self.assertEqual(by_id.route_name, "signals-fast")

        listed_r = await route_repo.list_routes()
        self.assertEqual(len(listed_r), 1)

        updated = await route_repo.update(
            route.id,
            target_model="claude-3-5",
            settings={"max_tokens": 2048},
        )
        self.assertEqual(updated.target_model, "claude-3-5")
        self.assertEqual(updated.settings, {"max_tokens": 2048})

        await route_repo.delete(route.id)
        with self.assertRaises(RecordNotFoundError):
            await route_repo.get_by_id(route.id)

    async def test_concurrent_instance_status_updates_keep_status_and_error_in_sync(self):
        base_repo = SqlAlchemyTaskRepository(self.session_factory)
        await base_repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            get_models=(Task,),
            execute_tables=("tasks",),
        )
        repo = SqlAlchemyTaskRepository(synchronized_factory)

        results = await asyncio.gather(
            repo.update_status("instance-1", "running", ""),
            repo.update_status("instance-1", "error", "boom"),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                raise result

        final = await base_repo.get_task("instance-1")
        self.assertIn(
            (final.status, final.last_error),
            {
                ("running", ""),
                ("error", "boom"),
            },
        )

    async def test_approval_repository_tracks_pending_resolution_and_expiry(self):
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        now = datetime(2026, 1, 1, 12, 0, 0)

        created = await repo.create_pending(
            approval_id="approval-1",
            intent_id="intent-1",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        pending = await repo.list_pending()
        resolved = await repo.resolve("approval-1", "approved", reason="operator approved")

        self.assertEqual(created.status, "pending")
        self.assertEqual([item.approval_id for item in pending], ["approval-1"])
        self.assertEqual(resolved.status, "approved")
        self.assertEqual(resolved.reason, "operator approved")
        self.assertEqual(await repo.list_pending(), [])

        with self.assertRaises(StateConflictError):
            await repo.resolve("approval-1", "rejected", reason="too late")

        await repo.create_pending(
            approval_id="approval-2",
            intent_id="intent-2",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=1),
        )
        expired = await repo.expire_pending(now + timedelta(minutes=2))
        pending_after_expiry = await repo.list_pending()

        self.assertEqual([item.approval_id for item in expired], ["approval-2"])
        self.assertEqual(pending_after_expiry, [])

    async def test_duplicate_approval_create_raises_repository_conflict(self):
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        now = datetime(2026, 1, 1, 12, 0, 0)

        await repo.create_pending(
            approval_id="approval-1",
            intent_id="intent-1",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        with self.assertRaises(StateConflictError):
            await repo.create_pending(
                approval_id="approval-1",
                intent_id="intent-2",
                mode="manual",
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            )

    async def test_approval_resolve_rejects_invalid_target_status_and_leaves_pending(self):
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        now = datetime(2026, 1, 1, 12, 0, 0)
        await repo.create_pending(
            approval_id="approval-1",
            intent_id="intent-1",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        with self.assertRaises(PersistenceError):
            await repo.resolve("approval-1", "pending", reason="bad")

        pending = await repo.list_pending()
        async with self.session_factory() as session:
            record = await session.get(ApprovalRecord, "approval-1")

        self.assertEqual([item.approval_id for item in pending], ["approval-1"])
        self.assertEqual(record.status, "pending")
        self.assertIsNone(record.resolved_at)

    async def test_concurrent_approval_resolution_allows_exactly_one_success(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        base_repo = SqlAlchemyApprovalRepository(self.session_factory)
        await base_repo.create_pending(
            approval_id="approval-1",
            intent_id="intent-1",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )

        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            execute_tables=("approvals",),
        )
        repo = SqlAlchemyApprovalRepository(synchronized_factory)

        results = await asyncio.gather(
            repo.resolve("approval-1", "approved", reason="operator approved"),
            repo.resolve("approval-1", "rejected", reason="operator rejected"),
            return_exceptions=True,
        )

        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, Exception)]

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], StateConflictError)
        self.assertEqual(await base_repo.list_pending(), [])
        async with self.session_factory() as session:
            record = await session.get(ApprovalRecord, "approval-1")
        self.assertIn(record.status, {"approved", "rejected"})
        self.assertIsNotNone(record.resolved_at)

    async def test_concurrent_approval_expiry_reports_each_transition_once(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        base_repo = SqlAlchemyApprovalRepository(self.session_factory)
        await base_repo.create_pending(
            approval_id="approval-1",
            intent_id="intent-1",
            mode="manual",
            created_at=now,
            expires_at=now + timedelta(minutes=1),
        )

        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            execute_tables=("approvals",),
            synchronize_scalars=True,
        )
        repo = SqlAlchemyApprovalRepository(synchronized_factory)
        expires_at = now + timedelta(minutes=2)

        results = await asyncio.gather(
            repo.expire_pending(expires_at),
            repo.expire_pending(expires_at),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                raise result

        flattened = [item.approval_id for batch in results for item in batch]

        self.assertEqual(sorted(len(batch) for batch in results), [0, 1])
        self.assertEqual(flattened, ["approval-1"])
        self.assertEqual(await base_repo.list_pending(), [])

    async def test_non_conflict_integrity_failures_raise_persistence_error_not_state_conflict(self):
        approval_repo = SqlAlchemyApprovalRepository(self.session_factory)
        now = datetime(2026, 1, 1, 12, 0, 0)

        approval_error = None
        try:
            await approval_repo.create_pending(
                approval_id="approval-1",
                intent_id="intent-1",
                mode=None,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        except Exception as error:  # noqa: BLE001
            approval_error = error

        self.assertIsInstance(approval_error, PersistenceError)
        self.assertNotIsInstance(approval_error, StateConflictError)

    async def test_system_state_repository_persists_global_kill_switch(self):
        repo = SqlAlchemySystemStateRepository(self.session_factory)

        initial = await repo.get_kill_switch_enabled()
        changed = await repo.set_kill_switch_enabled(True)
        persisted = await repo.get_kill_switch_enabled()

        self.assertFalse(initial)
        self.assertTrue(changed)
        self.assertTrue(persisted)

    async def test_concurrent_system_state_initialization_does_not_leak_integrity_error(self):
        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            get_models=(SystemStateRecord,),
        )
        repo = SqlAlchemySystemStateRepository(synchronized_factory)

        results = await asyncio.gather(
            repo.set_kill_switch_enabled(True),
            repo.set_kill_switch_enabled(False),
            return_exceptions=True,
        )

        for result in results:
            self.assertNotIsInstance(result, IntegrityError)
            if isinstance(result, Exception):
                raise result

        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(result, bool) for result in results))
        async with self.session_factory() as session:
            record = await session.get(SystemStateRecord, "global")
        self.assertIsNotNone(record)
        self.assertIn(record.kill_switch_enabled, {True, False})

    async def test_instance_repository_agent_fields_and_update_agent_config(self):
        repo = SqlAlchemyTaskRepository(self.session_factory)

        created = await repo.create_task(
            task_id="instance-1",
            name="alpha",
            mode="paper",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
            execution_strategy="langchain",
            account_id="acct-1",
            model_id="gpt-4",
            settings={"risk": "low"},
        )
        # The migrated fields are now stored IN settings.
        self.assertEqual(created.universe, ())
        self.assertEqual(created.execution_strategy, "langchain")
        self.assertEqual(created.account_id, "acct-1")
        self.assertEqual(created.model_id, "gpt-4")
        # settings contains the migrated fields plus original content.
        self.assertEqual(created.settings["risk"], "low")
        self.assertEqual(created.settings["execution_strategy"], "langchain")
        self.assertEqual(created.settings["account_id"], "acct-1")
        self.assertEqual(created.settings["model_id"], "gpt-4")

        got = await repo.get_task("instance-1")
        self.assertEqual(got.task_id, "instance-1")

        with self.assertRaises(RecordNotFoundError):
            await repo.update_agent_config(
                "missing",
                universe=["AAPL"],
            )

        updated = await repo.update_agent_config(
            "instance-1",
            model_id="gpt-4",
            settings=None,
        )
        # settings=None replaces settings with empty dict ({}).
        self.assertEqual(updated.settings, {})

        listed = await repo.list_tasks()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].task_id, "instance-1")

    async def test_debug_session_repository_tracks_status_events_and_lookup(self):
        repo = SqlAlchemyDebugSessionRepository(self.session_factory)

        created = await repo.create_session(
            session_id="debug-1",
            task_id="instance-1",
            config_overrides=None,
            input_overrides={"universe": ["600000.SH"]},
        )
        started = await repo.mark_running(
            "debug-1",
            run_id="run-1",
            effective_config={"react_max_turns": 3, "signal_tool_names": ["data_bars_relative"]},
        )
        first = await repo.append_event(
            "debug-1",
            "phase",
            {"phase": "run_strategy", "proposal_count": 1},
        )
        second = await repo.append_event(
            "debug-1",
            "signal_turn",
            {"turn": 1, "tool_call": {"tool": "data_bars_relative"}},
        )
        completed = await repo.mark_finished(
            "debug-1",
            status="completed",
            error_message="",
        )
        listed = await repo.list_sessions("instance-1")
        got = await repo.get_session("debug-1")
        events = await repo.list_events("debug-1")

        self.assertEqual(created.status, "pending")
        self.assertEqual(started.status, "running")
        self.assertEqual(started.run_id, "run-1")
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(listed), 1)
        self.assertEqual(got.session_id, "debug-1")
        self.assertEqual([event.event_type for event in events], ["phase", "signal_turn"])

    async def test_get_latest_session_accepts_aware_created_after(self):
        # Cron executors pass an aware cutoff (datetime.now(timezone.utc)); the repo
        # must normalize it to naive UTC so PostgreSQL TIMESTAMP WITHOUT TIME ZONE
        # columns accept the param (regression for the asyncpg naive/aware DataError).
        from datetime import datetime, timedelta, timezone

        repo = SqlAlchemyDebugSessionRepository(self.session_factory)
        await repo.create_session(
            session_id="debug-latest-1",
            task_id="instance-latest",
            config_overrides=None,
            input_overrides=None,
        )
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        # Aware cutoff in the past → session is newer → returned.
        got = await repo.get_latest_session("instance-latest", created_after=past)
        self.assertIsNotNone(got)
        self.assertEqual(got.session_id, "debug-latest-1")
        # Aware cutoff in the future → session is older → filtered out (no crash).
        none_got = await repo.get_latest_session("instance-latest", created_after=future)
        self.assertIsNone(none_got)

    async def test_debug_session_repository_reports_active_running_session(self):
        repo = SqlAlchemyDebugSessionRepository(self.session_factory)
        await repo.create_session(
            session_id="debug-1",
            task_id="instance-1",
            config_overrides=None,
            input_overrides=None,
        )
        await repo.mark_running("debug-1", run_id="run-1", effective_config={"react_max_turns": 1})

        active = await repo.get_active_session("instance-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.session_id, "debug-1")

    async def test_debug_session_repository_get_active_debug_session_ignores_scheduled(self):
        repo = SqlAlchemyDebugSessionRepository(self.session_factory)
        await repo.create_session(
            session_id="scheduled-instance-1",
            task_id="instance-1",
            config_overrides=None,
            input_overrides=None,
            session_type="scheduled",
        )
        self.assertIsNone(await repo.get_active_debug_session("instance-1"))

        await repo.create_session(
            session_id="debug-1",
            task_id="instance-1",
            config_overrides=None,
            input_overrides=None,
            session_type="debug",
        )
        active_debug = await repo.get_active_debug_session("instance-1")
        self.assertIsNotNone(active_debug)
        self.assertEqual(active_debug.session_id, "debug-1")

    async def test_cycle_run_repository_list_filters_and_counts(self):
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        base_t = datetime(2026, 4, 8, 12, 0, 0)
        async with self.session_factory() as session:
            rows = [
                ("r-a", "debug", "completed", base_t, "paper"),
                ("r-b", "scheduled", "running", base_t + timedelta(hours=1), "backtest"),
                ("r-c", "debug", "completed", base_t + timedelta(hours=2), "backtest"),
                ("run-find-me", "manual", "failed", base_t + timedelta(hours=3), "paper"),
            ]
            for run_id, run_kind, status, started, run_mode in rows:
                session.add(
                    CycleRunRecord(
                        run_id=run_id,
                        task_id="inst-cr-1",
                        agent_name="ag",
                        run_kind=run_kind,
                        run_mode=run_mode,
                        wall_started_at=started,
                        status=status,
                    )
                )
            await session.commit()

        items, total = await repo.list_for_task(
            "inst-cr-1",
            run_id_contains="find",
            status="failed",
            limit=10,
            offset=0,
        )
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["run_id"], "run-find-me")

        paged, total_all = await repo.list_for_task("inst-cr-1", limit=1, offset=0)
        self.assertEqual(total_all, 4)
        self.assertEqual(len(paged), 1)

        in_window, _ = await repo.list_for_task(
            "inst-cr-1",
            wall_started_at_after=base_t + timedelta(minutes=30),
            wall_started_at_before=base_t + timedelta(hours=1, minutes=30),
        )
        self.assertEqual(len(in_window), 1)
        self.assertEqual(in_window[0]["run_id"], "r-b")

        backtest_only, bt_total = await repo.list_for_task("inst-cr-1", run_mode="backtest")
        self.assertEqual(bt_total, 2)
        self.assertEqual({r["run_id"] for r in backtest_only}, {"r-b", "r-c"})

        backtest_no_debug, bnd_total = await repo.list_for_task(
            "inst-cr-1", run_mode="backtest", exclude_run_kind="debug"
        )
        self.assertEqual(bnd_total, 1)
        self.assertEqual(backtest_no_debug[0]["run_id"], "r-b")

    async def test_cycle_run_list_by_trace_id(self):
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        base_t = datetime(2026, 4, 9, 9, 0, 0)
        trace_a = "a" * 32
        trace_b = "b" * 32
        async with self.session_factory() as session:
            rows = [
                ("r-t1", trace_a, base_t + timedelta(minutes=2)),
                ("r-t0", trace_a, base_t),  # earlier — should sort first
                ("r-other", trace_b, base_t + timedelta(minutes=1)),
                ("r-none", None, base_t + timedelta(minutes=3)),
            ]
            for run_id, trace_id, started in rows:
                session.add(
                    CycleRunRecord(
                        run_id=run_id,
                        task_id="inst-trace",
                        agent_name="ag",
                        trace_id=trace_id,
                        wall_started_at=started,
                        status="completed",
                    )
                )
            await session.commit()

        matched = await repo.list_by_trace_id(trace_a)
        # Only the two trace_a rows, ordered by wall_started_at (chronological).
        self.assertEqual([r["run_id"] for r in matched], ["r-t0", "r-t1"])
        self.assertTrue(all(r["trace_id"] == trace_a for r in matched))

        self.assertEqual(await repo.list_by_trace_id("c" * 32), [])

    async def test_cycle_run_details_merge_and_api_strips_proposal_sizing(self):
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        await repo.create_started(
            run_id="run-det-1",
            task_id="inst-det",
            agent_name="ag",
            session_id=None,
            trace_id=None,
            run_mode="paper",
            run_kind="scheduled",
            clock_mode="wall",
            cycle_time=None,
            runtime_params=None,
        )
        await repo.finalize(
            "run-det-1",
            status="completed",
            details_patch={
                "universe": ["600000.SH"],
                "proposals": [{"symbol": "600000.SH", "side": "long", "quantity": 100, "amount": 1.0}],
            },
        )
        async with self.session_factory() as session:
            rec = await session.get(CycleRunRecord, "run-det-1")
            self.assertIsNotNone(rec)
            assert rec is not None
            assert rec.details is not None
            prop0 = rec.details["proposals"][0]
            self.assertEqual(prop0.get("quantity"), 100)
            self.assertEqual(prop0.get("amount"), 1.0)

        row = await repo.get_for_task("inst-det", "run-det-1")
        self.assertIsNotNone(row)
        assert row is not None
        det = row["details"]
        self.assertIsNotNone(det)
        api_prop = det["proposals"][0]
        self.assertNotIn("quantity", api_prop)
        self.assertNotIn("amount", api_prop)
        self.assertEqual(api_prop.get("symbol"), "600000.SH")

        await repo.finalize(
            "run-det-1",
            status="completed",
            details_patch={"universe": ["600000.SH", "601318.SH"]},
        )
        row2 = await repo.get_for_task("inst-det", "run-det-1")
        self.assertIsNotNone(row2)
        assert row2 is not None
        assert row2.get("details") is not None
        self.assertIn("proposals", row2["details"])
        self.assertEqual(row2["details"]["universe"], ["600000.SH", "601318.SH"])

        await repo.finalize(
            "run-det-1",
            status="completed",
            details_patch={
                "post_cycle_account": {
                    "source": "ledger",
                    "captured_at": "2026-04-18T08:00:00Z",
                    "account": {"cash": "1", "equity": "2"},
                    "total_market_value": "0",
                    "positions": [],
                }
            },
        )
        row3 = await repo.get_for_task("inst-det", "run-det-1")
        self.assertIsNotNone(row3)
        assert row3 is not None
        assert row3.get("details") is not None
        self.assertEqual(row3["details"]["post_cycle_account"]["source"], "ledger")
        self.assertIn("proposals", row3["details"])

    async def test_cycle_run_patch_details_merges_without_touching_status(self):
        # patch_details records the pushed card AFTER the cycle finalized; it must
        # merge into details and leave status / wall_finished_at / cycle_failed
        # untouched (unlike finalize, which is the cycle's terminal write).
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        await repo.create_started(
            run_id="run-patch-1",
            task_id="inst-patch",
            agent_name="ag",
            session_id=None,
            trace_id=None,
            run_mode="live",
            run_kind="trigger",
            clock_mode="wall",
            cycle_time=None,
            runtime_params=None,
        )
        await repo.finalize(
            "run-patch-1",
            status="completed",
            details_patch={"universe": ["600000.SH"]},
        )
        async with self.session_factory() as session:
            rec = await session.get(CycleRunRecord, "run-patch-1")
            assert rec is not None
            finished_before = rec.wall_finished_at

        await repo.patch_details(
            "run-patch-1",
            {"delivered_cards": [{"kind": "digest", "content": "# 卡片", "target_kind": "channel", "status": "forwarded"}]},
        )

        async with self.session_factory() as session:
            rec = await session.get(CycleRunRecord, "run-patch-1")
            assert rec is not None
            assert rec.details is not None
            # merged: both the pre-existing key and the new delivered_cards survive
            self.assertEqual(rec.details["universe"], ["600000.SH"])
            self.assertEqual(rec.details["delivered_cards"][0]["content"], "# 卡片")
            # status / finished time / failed flag untouched by the patch
            self.assertEqual(rec.status, "completed")
            self.assertEqual(rec.wall_finished_at, finished_before)
            self.assertFalse(rec.cycle_failed)

        # Missing run is a no-op (best-effort), not an error.
        await repo.patch_details("run-nope", {"x": 1})

    async def test_cycle_run_record_round_trips_signal_only_run_mode(self):
        """``run_mode='signal_only'`` must round-trip without losing the value.

        The ``cycle_runs.run_mode`` column is ``String(32)`` with no
        CheckConstraint — this asserts the contract holds end-to-end:
        create_started → SQLite → get_for_task and a direct ORM read both
        return the literal ``signal_only``. Also asserts position_intents
        with an empty fills list round-trips intact (matches the shape the
        worker writes in signal_only mode).
        """
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        await repo.create_started(
            run_id="run-signal-only-1",
            task_id="inst-signal-only",
            agent_name="ag",
            session_id=None,
            trace_id=None,
            run_mode="signal_only",
            run_kind="cron",
            clock_mode="wall",
            cycle_time=None,
            runtime_params=None,
        )
        await repo.finalize(
            "run-signal-only-1",
            status="completed",
            details_patch={
                "universe": ["A", "B"],
                "position_intents": [
                    {
                        "intent_id": "intent-A",
                        "symbol": "A",
                        "action": "buy",
                        "amount": "1000",
                        "price_reference": "10.0",
                    }
                ],
                "fills": [],
            },
            cycle_failed=False,
            failure_message="",
            submitted_count=0,
            vetoed_count=0,
            pending_approval_count=0,
        )

        # Direct ORM read keeps the literal value.
        async with self.session_factory() as session:
            rec = await session.get(CycleRunRecord, "run-signal-only-1")
            self.assertIsNotNone(rec)
            assert rec is not None
            self.assertEqual(rec.run_mode, "signal_only")
            self.assertEqual(rec.status, "completed")
            self.assertFalse(rec.cycle_failed)
            self.assertEqual(rec.submitted_count, 0)
            self.assertEqual(rec.vetoed_count, 0)
            self.assertEqual(rec.pending_approval_count, 0)
            assert rec.details is not None
            self.assertEqual(rec.details["fills"], [])
            self.assertEqual(len(rec.details["position_intents"]), 1)
            self.assertEqual(rec.details["position_intents"][0]["symbol"], "A")

        # Repository-level read (the API shape) returns the same.
        row = await repo.get_for_task("inst-signal-only", "run-signal-only-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["run_mode"], "signal_only")
        self.assertEqual(row["submitted_count"], 0)
        self.assertEqual(row["vetoed_count"], 0)

        # list_for_task filter narrowed by run_mode='signal_only' finds it.
        items, total = await repo.list_for_task(
            "inst-signal-only", run_mode="signal_only"
        )
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["run_id"], "run-signal-only-1")

    async def test_backtest_job_repository_crud_and_cycle_run_filter_by_job(self):
        job_repo = SqlAlchemyRunRepository(self.session_factory)
        cr_repo = SqlAlchemyCycleRunRepository(self.session_factory)
        base_t = datetime(2026, 4, 11, 8, 0, 0)
        await job_repo.create_pending(
            run_id="btjob-1",
            task_id="inst-bt",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1, 0, 0, 0),
            range_end_utc=datetime(2026, 1, 10, 0, 0, 0),
            session_id="backtest-s1",
            bars_total=3,
        )
        await job_repo.mark_running("btjob-1")
        await cr_repo.create_started(
            run_id="run-bt-1",
            task_id="inst-bt",
            agent_name="a",
            session_id="backtest-s1",
            trace_id=None,
            run_mode="backtest",
            run_kind="backtest_bar",
            clock_mode="simulated",
            cycle_time=base_t,
            runtime_params=None,
        )
        await cr_repo.finalize("run-bt-1", status="completed")
        row = await job_repo.get("btjob-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["bars_total"], 3)

        items, total = await cr_repo.list_for_task("inst-bt", session_id="backtest-s1")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["session_id"], "backtest-s1")

        listed, total_j = await job_repo.list_for_task("inst-bt")
        self.assertEqual(total_j, 1)
        self.assertEqual(listed[0]["run_id"], "btjob-1")

        await job_repo.finalize_success(
            "btjob-1",
            starting_equity=1000.0,
            ending_equity=1010.0,
            return_pct=1.0,
        )
        done = await job_repo.get("btjob-1")
        self.assertIsNotNone(done)
        assert done is not None
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["return_pct"], 1.0)

        async with self.session_factory() as session:
            session.add(
                Run(
                    run_id="btjob-2",
                    task_id="inst-bt",
                    mode="backtest",
                    status="pending",
                    market_profile="generic",
                    bar_interval="1d",
                    range_start_utc=datetime(2026, 2, 1, 0, 0, 0),
                    range_end_utc=datetime(2026, 2, 2, 0, 0, 0),
                    session_id=None,
                    error_message="",
                    bars_total=0,
                    bars_completed=0,
                )
            )
            await session.commit()
        self.assertTrue(await job_repo.has_active_job("inst-bt"))
        await job_repo.finalize_stopped("btjob-2")
        stopped = await job_repo.get("btjob-2")
        self.assertIsNotNone(stopped)
        assert stopped is not None
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(stopped["error_message"])
        self.assertFalse(await job_repo.has_active_job("inst-bt"))

    async def test_backtest_job_repository_list_jobs_global_and_filter(self):
        job_repo = SqlAlchemyRunRepository(self.session_factory)
        t0 = datetime(2026, 1, 1, 0, 0, 0)
        await job_repo.create_pending(
            run_id="btjob-global-a",
            task_id="inst-a",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=t0,
            range_end_utc=t0,
            session_id="s-a",
            bars_total=1,
        )
        await job_repo.create_pending(
            run_id="btjob-global-b",
            task_id="inst-b",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=t0,
            range_end_utc=t0,
            session_id="s-b",
            bars_total=1,
        )
        all_items, all_total = await job_repo.list_jobs(None, limit=10, offset=0)
        self.assertEqual(all_total, 2)
        self.assertEqual(len(all_items), 2)
        a_items, a_total = await job_repo.list_jobs("inst-a", limit=10, offset=0)
        self.assertEqual(a_total, 1)
        self.assertEqual(a_items[0]["task_id"], "inst-a")
        one_item, two_total = await job_repo.list_jobs(None, limit=1, offset=1)
        self.assertEqual(two_total, 2)
        self.assertEqual(len(one_item), 1)
        self.assertEqual(one_item[0]["run_id"], "btjob-global-a")

    async def test_backtest_job_repository_pause_resume_and_active_flag(self):
        job_repo = SqlAlchemyRunRepository(self.session_factory)
        await job_repo.create_pending(
            run_id="btjob-pause-1",
            task_id="inst-bt-pause",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1, 0, 0, 0),
            range_end_utc=datetime(2026, 1, 5, 0, 0, 0),
            session_id="backtest-sp",
            bars_total=2,
        )
        await job_repo.mark_running("btjob-pause-1")
        self.assertTrue(await job_repo.has_active_job("inst-bt-pause"))
        await job_repo.mark_paused("btjob-pause-1")
        paused = await job_repo.get("btjob-pause-1")
        self.assertIsNotNone(paused)
        assert paused is not None
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(await job_repo.has_active_job("inst-bt-pause"))
        await job_repo.mark_resumed("btjob-pause-1")
        running = await job_repo.get("btjob-pause-1")
        self.assertIsNotNone(running)
        assert running is not None
        self.assertEqual(running["status"], "running")
        await job_repo.finalize_success(
            "btjob-pause-1",
            starting_equity=1.0,
            ending_equity=1.0,
            return_pct=0.0,
        )
        self.assertFalse(await job_repo.has_active_job("inst-bt-pause"))

    async def test_backtest_job_update_running_metrics_while_running(self):
        job_repo = SqlAlchemyRunRepository(self.session_factory)
        await job_repo.create_pending(
            run_id="btjob-mtm",
            task_id="inst-mtm",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1, 0, 0, 0),
            range_end_utc=datetime(2026, 1, 5, 0, 0, 0),
            session_id="backtest-mtm",
            bars_total=3,
        )
        await job_repo.mark_running("btjob-mtm")
        await job_repo.update_running_metrics(
            "btjob-mtm",
            starting_equity=100_000.0,
            ending_equity=101_000.0,
            return_pct=1.0,
        )
        row = await job_repo.get("btjob-mtm")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["starting_equity"], 100_000.0)
        self.assertEqual(row["ending_equity"], 101_000.0)
        self.assertEqual(row["return_pct"], 1.0)

        await job_repo.finalize_success(
            "btjob-mtm",
            starting_equity=100_000.0,
            ending_equity=102_000.0,
            return_pct=2.0,
        )
        await job_repo.update_running_metrics(
            "btjob-mtm",
            starting_equity=1.0,
            ending_equity=2.0,
            return_pct=99.0,
        )
        done = await job_repo.get("btjob-mtm")
        self.assertIsNotNone(done)
        assert done is not None
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["return_pct"], 2.0)

    async def test_backtest_job_checkpoint_stop_flag_and_recovery_list(self):
        job_repo = SqlAlchemyRunRepository(self.session_factory)
        await job_repo.create_pending(
            run_id="btjob-chk",
            task_id="inst-chk",
            mode="backtest",
            market_profile="cn_a_share",
            bar_interval="1d",
            range_start_utc=datetime(2026, 1, 1, 0, 0, 0),
            range_end_utc=datetime(2026, 1, 5, 0, 0, 0),
            session_id="backtest-chk",
            bars_total=3,
        )
        await job_repo.mark_running("btjob-chk")
        payload = {"symbol_to_price": {"600000.SH": 12.3}, "cash": 90000.0, "positions": []}
        await job_repo.save_ledger_checkpoint("btjob-chk", payload)
        await job_repo.set_stop_requested("btjob-chk", True)
        row = await job_repo.get("btjob-chk")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(row["stop_requested"])
        self.assertEqual(row["ledger_checkpoint_json"], payload)

        listed = await job_repo.list_jobs_with_statuses(("running",))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["run_id"], "btjob-chk")

        await job_repo.set_reference_starting_equity_once("btjob-chk", 100_000.0)
        await job_repo.set_reference_starting_equity_once("btjob-chk", 1.0)
        row2 = await job_repo.get("btjob-chk")
        assert row2 is not None
        self.assertEqual(row2["reference_starting_equity"], 100_000.0)

        await job_repo.mark_paused_shutdown("btjob-chk")
        paused = await job_repo.get("btjob-chk")
        assert paused is not None
        self.assertEqual(paused["status"], "paused")

    # ── Aware-UTC acceptance for naive DateTime columns ────────────────
    #
    # Regression family from the cron incident: callers in our codebase
    # routinely emit ``datetime.now(timezone.utc)`` but the columns under
    # these repos are SQLAlchemy ``DateTime`` (= ``TIMESTAMP WITHOUT TIME
    # ZONE`` on Postgres). asyncpg refuses aware values. SQLite ignores
    # tz so dev-time tests never noticed. Each writer that takes a
    # datetime kwarg now strips at the boundary via ``_to_naive_utc``;
    # these tests fix that contract so a future regression — handing an
    # aware datetime through — fails loudly here instead of in prod.

    async def test_approval_create_pending_accepts_aware_utc(self):
        # ApprovalRecord.intent_id is a plain string column (no FK), so the
        # bare repo write is enough to exercise the aware-datetime path.
        repo = SqlAlchemyApprovalRepository(self.session_factory)
        now_aware = datetime.now(timezone.utc)
        await repo.create_pending(
            approval_id="ap-aware",
            intent_id="intent-aware",
            mode="paper",
            created_at=now_aware,
            expires_at=now_aware + timedelta(minutes=5),
        )
        # And expire_pending with an aware ``now`` must also succeed.
        expired = await repo.expire_pending(now_aware + timedelta(minutes=10))
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].approval_id, "ap-aware")

    async def test_trade_fill_insert_accepts_aware_filled_at(self):
        repo = SqlAlchemyTradeFillRepository(self.session_factory)
        ok = await repo.insert_fill(
            task_id="task-aware",
            cycle_run_id="run-aware",
            symbol="600000.SH",
            side="buy",
            quantity="1",
            price="10",
            amount="10",
            fee=None,
            currency=None,
            intent_id="intent-aware-fill",
            rationale="",
            filled_at=datetime.now(timezone.utc),
            source_mode="backtest",
            raw_payload=None,
        )
        self.assertTrue(ok)

    async def test_cycle_run_create_started_accepts_aware_cycle_time(self):
        # CycleRunRecord.task_id is a plain string column with no FK on
        # this branch, so the bare write exercises the aware path without
        # needing to seed a parent task.
        repo = SqlAlchemyCycleRunRepository(self.session_factory)
        await repo.create_started(
            run_id="run-cyc-aware",
            task_id="t-cyc-aware",
            agent_name="alpha",
            session_id=None,
            trace_id=None,
            run_mode="paper",
            run_kind="scheduled",
            clock_mode="wall",
            cycle_time=datetime.now(timezone.utc),
            runtime_params=None,
        )

    async def test_run_repository_create_pending_accepts_aware_ranges(self):
        repo = SqlAlchemyRunRepository(self.session_factory)
        now_aware = datetime.now(timezone.utc)
        await repo.create_pending(
            run_id="run-aware-pending",
            task_id="t-run-aware",
            mode="backtest",
            market_profile="cn",
            bar_interval="1d",
            range_start_utc=now_aware,
            range_end_utc=now_aware + timedelta(days=30),
            session_id="sess-aware",
            bars_total=20,
        )

    async def test_run_repository_debug_enabled_and_overrides_roundtrip(self):
        repo = SqlAlchemyRunRepository(self.session_factory)
        now = datetime.now(timezone.utc)
        # Default: debug_enabled True, session present.
        await repo.create_pending(
            run_id="run-debug-on",
            task_id="t-debug",
            mode="backtest",
            market_profile="cn",
            bar_interval="1d",
            range_start_utc=now,
            range_end_utc=now + timedelta(days=10),
            session_id="sess-on",
            bars_total=5,
        )
        # Fast mode: debug_enabled False, no session, overrides persisted on run.
        await repo.create_pending(
            run_id="run-debug-off",
            task_id="t-debug",
            mode="backtest",
            market_profile="cn",
            bar_interval="1d",
            range_start_utc=now,
            range_end_utc=now + timedelta(days=10),
            session_id=None,
            bars_total=5,
            debug_enabled=False,
            config_overrides_json={"universe": ["600519.SH"]},
        )
        on_row = await repo.get("run-debug-on")
        off_row = await repo.get("run-debug-off")
        self.assertEqual(on_row["debug_enabled"], True)
        self.assertIsNone(on_row["config_overrides_json"])
        self.assertEqual(off_row["debug_enabled"], False)
        self.assertIsNone(off_row["session_id"])
        self.assertEqual(off_row["config_overrides_json"], {"universe": ["600519.SH"]})

    async def test_debug_session_span_append_accepts_aware_times(self):
        from doyoutrade.persistence.models import DebugSessionRecord
        from doyoutrade.persistence.repositories import SqlAlchemyDebugSessionSpanRepository
        # Seed a parent debug session for the FK.
        async with self.session_factory() as session:
            session.add(DebugSessionRecord(
                session_id="dbg-aware",
                task_id="dbg-task",
                status="pending",
                config_overrides=None,
                input_overrides=None,
                session_type="debug",
            ))
            await session.commit()
        repo = SqlAlchemyDebugSessionSpanRepository(self.session_factory)
        now_aware = datetime.now(timezone.utc)
        await repo.append_span(
            span_id="span-aware",
            trace_id="trace-aware",
            parent_span_id=None,
            session_id="dbg-aware",
            name="root",
            span_type="debug",
            start_time=now_aware,
            end_time=now_aware + timedelta(milliseconds=50),
            duration_ms=50.0,
            attributes={"k": "v"},
            status="ok",
        )


class MockTradingLedgerCheckpointTests(unittest.TestCase):
    def test_ledger_checkpoint_roundtrip(self):
        from decimal import Decimal

        from doyoutrade.data.mock_provider import MockTradingDataProvider
        from doyoutrade.core.models import PositionSnapshot
        from doyoutrade.money.decimal_helpers import decimal_from_number

        m = MockTradingDataProvider(
            cash=50_000.0,
            equity=50_000.0,
            positions=[PositionSnapshot(symbol="X.SH", quantity=10.0, cost_price=5.0)],
        )
        m._symbol_to_price["X.SH"] = decimal_from_number(7.0)
        cp = m.ledger_checkpoint()
        m2 = MockTradingDataProvider()
        m2.restore_ledger_checkpoint(cp)

        async def _read():
            snap = await m2.get_account_snapshot()
            pos = await m2.get_positions()
            return snap, pos

        snap, pos = asyncio.run(_read())
        self.assertEqual(snap.cash, Decimal("50000"))
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].symbol, "X.SH")
        self.assertAlmostEqual(pos[0].quantity, 10.0)
        self.assertEqual(pos[0].cost_price, Decimal("5"))


class CronPreActionAndRunsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_cron_job_effective_status_derives_from_enabled(self):
        # Freshly-created jobs have ``last_status IS NULL`` until the first
        # fire stamps a value. The repository's serializer must surface a
        # non-null ``effective_status`` so the list page never shows the
        # ambiguous "—" placeholder.
        from doyoutrade.persistence.repositories import (
            SqlAlchemyCronJobRepository,
            _cron_job_effective_status,
        )

        async with self.session_factory() as session:
            session.add(AgentRecord(id="ax", name="agent", system_prompt=""))
            await session.commit()
        repo = SqlAlchemyCronJobRepository(self.session_factory)

        enabled_job = await repo.upsert_job({
            "agent_id": "ax",
            "name": "enabled-no-run",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "enabled": True,
            "task_kind": "agent_chat_reply",
        })
        self.assertIsNone(enabled_job["last_status"])
        self.assertEqual(enabled_job["effective_status"], "waiting")

        disabled_job = await repo.upsert_job({
            "agent_id": "ax",
            "name": "disabled-no-run",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "enabled": False,
            "task_kind": "agent_chat_reply",
        })
        self.assertEqual(disabled_job["effective_status"], "paused")

        # Once a fire writes ``last_status``, it wins over the derivation.
        async with self.session_factory() as session:
            row = await session.get(CronJobRecord, enabled_job["id"])
            assert row is not None
            row.last_status = "success"
            await session.commit()
        with_status = await repo.get_job(enabled_job["id"])
        assert with_status is not None
        self.assertEqual(with_status["effective_status"], "success")

        # Spot-check the bare helper too (used by the API serializer
        # directly elsewhere if needed).
        async with self.session_factory() as session:
            row = await session.get(CronJobRecord, disabled_job["id"])
            assert row is not None
            self.assertEqual(_cron_job_effective_status(row), "paused")

    async def test_cron_job_update_round_trips_string_timestamps(self):
        # Regression: cron_manager.update_job builds merged = {**existing,
        # **updates}; ``existing`` came from _cron_job_dict with ISO-STRING
        # last_run_at/created_at/updated_at. Feeding those back into upsert_job
        # must re-parse them to datetime — asyncpg rejects a str bound to a
        # TIMESTAMP column (PostgreSQL 500; SQLite silently tolerated it).
        from doyoutrade.persistence.repositories import (
            SqlAlchemyCronJobRepository,
            _coerce_naive_utc_dt,
        )

        # Bare coercer: datetime, ISO string, and None all normalize to naive UTC.
        self.assertIsNone(_coerce_naive_utc_dt(None))
        aware = datetime(2026, 6, 3, 6, 55, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _coerce_naive_utc_dt(aware), aware.replace(tzinfo=None),
        )
        self.assertEqual(
            _coerce_naive_utc_dt("2026-06-03T06:55:00"),
            datetime(2026, 6, 3, 6, 55, 0),
        )

        async with self.session_factory() as session:
            session.add(AgentRecord(id="ay", name="agent", system_prompt=""))
            await session.commit()
        repo = SqlAlchemyCronJobRepository(self.session_factory)

        created = await repo.upsert_job({
            "agent_id": "ay",
            "name": "fired-job",
            "cron_expression": "*/5 * * * *",
            "timezone": "UTC",
            "enabled": True,
            "task_kind": "strategy_signal_alert",
            "task_params_json": {"strategy_task_ids": ["task-1"]},
        })
        # Simulate a fire stamping last_run_at, then read the serialized dict
        # (ISO strings) exactly as cron_manager.update_job's ``existing`` is.
        await repo.update_job_state(
            created["id"], last_run_at=datetime.now(timezone.utc),
            last_status="success",
        )
        existing = await repo.get_job(created["id"])
        assert existing is not None
        self.assertIsInstance(existing["last_run_at"], str)  # ISO string

        # The merged-dict upsert (string timestamps + a real change) must not
        # raise and must persist the new task_params.
        merged = {
            **existing,
            "task_params_json": {
                "strategy_task_ids": ["task-1"], "no_signal_mode": "full",
            },
        }
        updated = await repo.upsert_job(merged)
        self.assertEqual(
            updated["task_params_json"]["no_signal_mode"], "full",
        )
        self.assertIsNotNone(updated["last_run_at"])

    async def test_cron_job_pre_action_nullable(self):
        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(
                CronJobRecord(
                    id="c1",
                    agent_id="a1",
                    name="n",
                    cron_expression="* * * * *",
                    input_template="t",
                )
            )
            await session.commit()
            session.expire_all()
            loaded = await session.get(CronJobRecord, "c1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertIsNone(loaded.pre_action)

    async def test_cron_job_run_record_round_trip(self):
        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(
                CronJobRecord(
                    id="c1",
                    agent_id="a1",
                    name="n",
                    cron_expression="* * * * *",
                    input_template="t",
                )
            )
            await session.commit()
            run = CronJobRunRecord(
                id="r1",
                job_id="c1",
                fired_at=datetime.utcnow(),
                started_at=datetime.utcnow(),
                status="running",
            )
            session.add(run)
            await session.commit()
            session.expire_all()
            loaded = await session.get(CronJobRunRecord, "r1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.job_id, "c1")
            self.assertEqual(loaded.status, "running")
            self.assertIsNone(loaded.pre_result_json)
            # New column defaults to NULL ("untraced" / legacy row).
            self.assertIsNone(loaded.trace_id)

    async def test_cron_job_run_trace_id_persist_and_reverse_lookup(self):
        from doyoutrade.persistence.repositories import SqlAlchemyCronJobRunRepository

        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(
                CronJobRecord(
                    id="c1", agent_id="a1", name="n",
                    cron_expression="* * * * *", input_template="t",
                )
            )
            await session.commit()

        repo = SqlAlchemyCronJobRunRepository(self.session_factory)
        base_t = datetime(2026, 5, 1, 8, 0, 0)
        trace = "a" * 32
        await repo.create_run({"id": "crun-1", "job_id": "c1", "fired_at": base_t})
        await repo.create_run({"id": "crun-2", "job_id": "c1", "fired_at": base_t + timedelta(minutes=5)})

        # trace_id rides the update whitelist; both fires share one trace here.
        updated = await repo.update_run("crun-1", {"trace_id": trace})
        self.assertEqual(updated["trace_id"], trace)
        await repo.update_run("crun-2", {"trace_id": trace})

        # get_run / list_for_job surface trace_id in the serialized dict.
        got = await repo.get_run("crun-1")
        self.assertEqual(got["trace_id"], trace)

        matched = await repo.list_by_trace_id(trace)
        # Newest first.
        self.assertEqual([r["id"] for r in matched], ["crun-2", "crun-1"])
        self.assertTrue(all(r["trace_id"] == trace for r in matched))
        self.assertEqual(await repo.list_by_trace_id("b" * 32), [])

    async def test_cron_job_run_cascades_on_parent_delete(self):
        """Deleting a CronJobRecord cascades to its CronJobRunRecords."""
        # Enable SQLite FK enforcement on this engine so ON DELETE CASCADE fires.
        from sqlalchemy import event

        sync_engine = self.engine.sync_engine

        def _fk_on(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        event.listen(sync_engine, "connect", _fk_on)
        try:
            # Re-open one connection so the pragma applies to subsequent sessions
            # (NullPool means each session opens a fresh connection that picks up the listener).
            async with self.session_factory() as session:
                session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
                await session.commit()
                session.add(
                    CronJobRecord(
                        id="c1",
                        agent_id="a1",
                        name="n",
                        cron_expression="* * * * *",
                        input_template="t",
                    )
                )
                await session.commit()
                session.add(
                    CronJobRunRecord(
                        id="r1",
                        job_id="c1",
                        fired_at=datetime.utcnow(),
                        started_at=datetime.utcnow(),
                        status="running",
                    )
                )
                await session.commit()

                parent = await session.get(CronJobRecord, "c1")
                self.assertIsNotNone(parent)
                await session.delete(parent)
                await session.commit()
                session.expire_all()

                self.assertIsNone(await session.get(CronJobRunRecord, "r1"))
        finally:
            event.remove(sync_engine, "connect", _fk_on)


class ListSessionsFilterDialectTests(unittest.TestCase):
    """`config` JSON extraction must compile per-dialect, not via the
    SQLite-only ``json_extract`` SQL function (which asyncpg rejects with
    UndefinedFunctionError)."""

    def _compile(self, dialect, *, channel_id=None, source=None):
        from sqlalchemy.dialects import postgresql, sqlite
        from doyoutrade.assistant.repository import _build_list_sessions_filters
        from doyoutrade.persistence.models import AssistantSessionRecord

        dial = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect]
        stmt = select(func.count()).select_from(AssistantSessionRecord)
        for clause in _build_list_sessions_filters(channel_id=channel_id, source=source):
            stmt = stmt.where(clause)
        return str(stmt.compile(dialect=dial)).lower()

    def test_postgresql_uses_path_operator_not_json_extract(self):
        sql = self._compile("postgresql", channel_id="channel-abc")
        self.assertNotIn("json_extract", sql)
        self.assertIn("#>>", sql)

    def test_postgresql_channel_source_predicate_avoids_json_extract(self):
        sql = self._compile("postgresql", source="channel")
        self.assertNotIn("json_extract", sql)
        self.assertIn("#>>", sql)

    def test_sqlite_still_uses_json_extract(self):
        sql = self._compile("sqlite", channel_id="channel-abc")
        self.assertIn("json_extract", sql)


class ObservabilityTtlPruneTests(unittest.IsolatedAsyncioTestCase):
    """The retention TTL prune deletes aged observability rows but keeps cycle_runs."""

    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    @staticmethod
    def _now() -> datetime:
        # Match models._utcnow (naive UTC) so the age comparison is apples-to-apples.
        return datetime.now(timezone.utc).replace(tzinfo=None)

    async def _seed(self, *, suffix: str, ts: datetime) -> None:
        async with self.session_factory() as session:
            session.add(
                DebugSessionRecord(
                    session_id=f"sess-{suffix}",
                    task_id="task-1",
                    status="completed",
                    created_at=ts,
                )
            )
            session.add(
                DebugSessionEventRecord(
                    session_id=f"sess-{suffix}",
                    sequence=1,
                    event_type="phase",
                    payload={},
                    timestamp=ts,
                )
            )
            session.add(
                DebugSessionSpanRecord(
                    span_id=f"span-{suffix}",
                    trace_id=f"trace-{suffix}",
                    session_id=f"sess-{suffix}",
                    name="worker.phase.x",
                    span_type="phase",
                    start_time=ts,
                )
            )
            session.add(
                ModelInvocationRecord(
                    created_at=ts,
                    model_id="m",
                    provider_kind="anthropic",
                    model="m",
                    call_kind="chat",
                    ok=True,
                    request_payload={},
                )
            )
            # cycle_runs row — the durable record that must NEVER be pruned,
            # even when older than the window.
            session.add(
                CycleRunRecord(
                    run_id=f"run-{suffix}",
                    task_id="task-1",
                    session_id=f"sess-{suffix}",
                    trace_id=f"trace-{suffix}",
                    wall_started_at=ts,
                )
            )
            await session.commit()

    async def _count(self, model) -> int:
        async with self.session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)

    async def test_prune_deletes_aged_rows_and_preserves_cycle_runs(self):
        from doyoutrade.persistence.observability_ttl_prune import prune_observability_rows

        now = self._now()
        await self._seed(suffix="old", ts=now - timedelta(days=40))
        await self._seed(suffix="new", ts=now - timedelta(days=1))

        counts = await prune_observability_rows(self.session_factory, ttl_days=7)

        # Exactly the four observability tables, each losing its single aged row.
        self.assertEqual(
            counts,
            {
                "debug_session_events": 1,
                "debug_session_spans": 1,
                "model_invocations": 1,
                "debug_sessions": 1,
            },
        )
        # Fresh rows survive.
        self.assertEqual(await self._count(DebugSessionRecord), 1)
        self.assertEqual(await self._count(DebugSessionEventRecord), 1)
        self.assertEqual(await self._count(DebugSessionSpanRecord), 1)
        self.assertEqual(await self._count(ModelInvocationRecord), 1)
        # Both cycle_runs rows survive — including the 40-day-old one.
        self.assertEqual(await self._count(CycleRunRecord), 2)

    async def test_prune_rejects_non_positive_ttl(self):
        from doyoutrade.persistence.observability_ttl_prune import prune_observability_rows

        with self.assertRaises(ValueError):
            await prune_observability_rows(self.session_factory, ttl_days=0)
        with self.assertRaises(ValueError):
            await prune_observability_rows(self.session_factory, ttl_days=-3)
