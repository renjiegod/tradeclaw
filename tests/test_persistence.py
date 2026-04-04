import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.dml import Update

from tradeclaw.persistence.db import create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError
from tradeclaw.persistence.models import AgentInstance, ApprovalRecord, Base, SystemStateRecord
from tradeclaw.persistence.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyInstanceRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTraceEventRepository,
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

    def test_persistence_metadata_exposes_expected_runtime_tables(self):
        self.assertEqual(
            set(Base.metadata.tables),
            {"instances", "approvals", "trace_events", "system_state"},
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
                instance_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('instances')")
                }
                approval_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('approvals')")
                }
                system_state_defaults = {
                    row[1]: row[4] for row in connection.execute("PRAGMA table_info('system_state')")
                }

            self.assertTrue(
                {"instances", "approvals", "trace_events", "system_state"}.issubset(tables)
            )
            self.assertTrue(
                {
                    "ix_approvals_status",
                    "ix_approvals_expires_at",
                    "ix_approvals_status_expires_at",
                }.issubset(indexes)
            )
            self.assertEqual(instance_defaults["description"], "''")
            self.assertEqual(instance_defaults["last_error"], "''")
            self.assertEqual(approval_defaults["reason"], "''")
            self.assertEqual(system_state_defaults["kill_switch_enabled"], "0")

    async def test_db_factory_returns_real_async_sqlalchemy_primitives(self):
        self.assertIsInstance(self.engine, AsyncEngine)

        async with self.session_factory() as session:
            self.assertIsInstance(session, AsyncSession)

    async def test_instance_repository_persists_status_transitions(self):
        repo = SqlAlchemyInstanceRepository(self.session_factory)

        created = await repo.create_instance(
            instance_id="instance-1",
            name="alpha",
            template_id="single-agent-trend",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        updated = await repo.update_status("instance-1", "running", "")

        self.assertEqual(created.instance_id, "instance-1")
        self.assertEqual(updated.status, "running")

    async def test_duplicate_instance_create_raises_repository_conflict(self):
        repo = SqlAlchemyInstanceRepository(self.session_factory)

        await repo.create_instance(
            instance_id="instance-1",
            name="alpha",
            template_id="single-agent-trend",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        with self.assertRaises(StateConflictError):
            await repo.create_instance(
                instance_id="instance-1",
                name="alpha-duplicate",
                template_id="single-agent-trend",
                mode="paper",
                orchestrator_mode="single-agent",
                description="demo",
                data_provider="mock",
                status="configured",
                last_error="",
            )

    async def test_instance_repository_lists_and_resolves_instances(self):
        repo = SqlAlchemyInstanceRepository(self.session_factory)

        first = await repo.create_instance(
            instance_id="instance-1",
            name="alpha",
            template_id="single-agent-trend",
            mode="paper",
            orchestrator_mode="single-agent",
            description="first",
            data_provider="mock",
            status="configured",
            last_error="",
        )
        second = await repo.create_instance(
            instance_id="instance-2",
            name="beta",
            template_id="single-agent-event",
            mode="paper",
            orchestrator_mode="single-agent",
            description="second",
            data_provider=None,
            status="paused",
            last_error="",
        )

        listed = await repo.list_instances()
        by_id = await repo.get_instance(first.instance_id)
        by_name = await repo.get_instance(second.name)

        self.assertEqual([item.instance_id for item in listed], ["instance-1", "instance-2"])
        self.assertEqual(by_id.name, "alpha")
        self.assertEqual(by_name.instance_id, "instance-2")

    async def test_instance_repository_raises_for_missing_instance(self):
        repo = SqlAlchemyInstanceRepository(self.session_factory)

        with self.assertRaises(RecordNotFoundError):
            await repo.update_status("missing", "running", "")

        with self.assertRaises(RecordNotFoundError):
            await repo.get_instance("missing")

    async def test_concurrent_instance_status_updates_keep_status_and_error_in_sync(self):
        base_repo = SqlAlchemyInstanceRepository(self.session_factory)
        await base_repo.create_instance(
            instance_id="instance-1",
            name="alpha",
            template_id="single-agent-trend",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
        )

        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            get_models=(AgentInstance,),
            execute_tables=("instances",),
        )
        repo = SqlAlchemyInstanceRepository(synchronized_factory)

        results = await asyncio.gather(
            repo.update_status("instance-1", "running", ""),
            repo.update_status("instance-1", "error", "boom"),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                raise result

        final = await base_repo.get_instance("instance-1")
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

    async def test_trace_event_repository_appends_ordered_sequences_per_run(self):
        repo = SqlAlchemyTraceEventRepository(self.session_factory)

        first = await repo.append_event("run-1", "load_context", {"ok": True})
        second = await repo.append_event("run-1", "dispatch_orders", {"count": 1})
        other = await repo.append_event("run-2", "load_context", {"ok": True})
        run_events = await repo.list_run_events("run-1")

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(other.sequence, 1)
        self.assertEqual([event.sequence for event in run_events], [1, 2])

    async def test_concurrent_trace_appends_retry_without_leaking_integrity_error(self):
        synchronized_factory = _SynchronizedSessionFactory(
            self.session_factory,
            synchronize_scalar=True,
            parties=5,
        )
        repo = SqlAlchemyTraceEventRepository(synchronized_factory)

        results = await asyncio.gather(
            *(repo.append_event("run-1", f"phase-{index}", {"index": index}) for index in range(5)),
            return_exceptions=True,
        )

        for result in results:
            self.assertNotIsInstance(result, IntegrityError)
            if isinstance(result, Exception):
                raise result

        sequences = sorted(result.sequence for result in results)
        run_events = await SqlAlchemyTraceEventRepository(self.session_factory).list_run_events("run-1")

        self.assertEqual(sequences, [1, 2, 3, 4, 5])
        self.assertEqual([event.sequence for event in run_events], [1, 2, 3, 4, 5])

    async def test_non_conflict_integrity_failures_raise_persistence_error_not_state_conflict(self):
        approval_repo = SqlAlchemyApprovalRepository(self.session_factory)
        trace_repo = SqlAlchemyTraceEventRepository(self.session_factory)
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

        trace_error = None
        try:
            await trace_repo.append_event("run-1", None, {"ok": True})
        except Exception as error:  # noqa: BLE001
            trace_error = error

        self.assertIsInstance(approval_error, PersistenceError)
        self.assertNotIsInstance(approval_error, StateConflictError)
        self.assertIsInstance(trace_error, PersistenceError)
        self.assertNotIsInstance(trace_error, StateConflictError)

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
        repo = SqlAlchemyInstanceRepository(self.session_factory)

        created = await repo.create_instance(
            instance_id="instance-1",
            name="alpha",
            template_id="single-agent-trend",
            mode="paper",
            orchestrator_mode="single-agent",
            description="demo",
            data_provider="mock",
            status="configured",
            last_error="",
            watch_symbols=["AAPL", "MSFT"],
            execution_strategy="langchain",
            account_id="acct-1",
            model_id="gpt-4",
            settings={"risk": "low"},
        )
        self.assertEqual(created.watch_symbols, ("AAPL", "MSFT"))
        self.assertEqual(created.execution_strategy, "langchain")
        self.assertEqual(created.account_id, "acct-1")
        self.assertEqual(created.model_id, "gpt-4")
        self.assertEqual(created.settings, {"risk": "low"})

        got = await repo.get_instance("instance-1")
        self.assertEqual(got.instance_id, "instance-1")

        with self.assertRaises(RecordNotFoundError):
            await repo.update_agent_config(
                "missing",
                watch_symbols=["AAPL"],
            )

        updated = await repo.update_agent_config(
            "instance-1",
            watch_symbols=["NVDA"],
            model_id="gpt-4",
            settings=None,
        )
        self.assertEqual(updated.watch_symbols, ("NVDA",))
        self.assertIsNone(updated.settings)

        listed = await repo.list_instances()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].instance_id, "instance-1")
        self.assertEqual(listed[0].watch_symbols, ("NVDA",))
