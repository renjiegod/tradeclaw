# Unified Async Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a unified async SQLAlchemy persistence layer with SQLite-by-default and Alembic migrations, then move instances, approvals, trace events, and kill-switch state onto durable storage with restart recovery.

**Architecture:** Introduce an async persistence package under `tradeclaw.persistence` that owns engine/session setup, ORM models, and repository implementations. Refactor bootstrap, platform service, approval gate, trace store, runtime loop, scheduler, and API endpoints to use async repository-backed state while keeping `RuntimeScheduler` responsible only for in-process worker execution.

**Tech Stack:** Python 3.12, unittest, FastAPI, SQLAlchemy async ORM, Alembic, aiosqlite, SQLite, uv

---

## File Structure

### Create

- `tradeclaw/persistence/db.py` - async engine, sessionmaker, declarative base, engine lifecycle
- `tradeclaw/persistence/models.py` - ORM models for instances, approvals, trace events, and system state
- `tradeclaw/persistence/repositories.py` - async repository interfaces and SQLAlchemy implementations
- `tradeclaw/persistence/runtime_state.py` - async trace store and persistence bootstrap helpers
- `tradeclaw/persistence/errors.py` - storage-specific exceptions translated into business-meaningful failures
- `tests/test_persistence.py` - repository and trace store tests against temporary SQLite databases
- `alembic.ini` - Alembic configuration
- `alembic/env.py` - Alembic metadata wiring for async SQLAlchemy models
- `alembic/script.py.mako` - Alembic revision template
- `alembic/versions/20260403_01_unified_async_storage.py` - initial migration creating runtime tables

### Modify

- `pyproject.toml` - add SQLAlchemy, Alembic, and async SQLite driver dependencies
- `tradeclaw/default_config.yaml` - add `database` defaults
- `tradeclaw/config.py` - parse `DatabaseSettings`
- `tradeclaw/persistence/__init__.py` - export async persistence entrypoints
- `tradeclaw/persistence/trace_store.py` - replace in-memory-only implementation with async repository-backed trace store while keeping an in-memory test double
- `tradeclaw/execution/approval.py` - convert `QueuedApprovalGate` to async repository-backed behavior
- `tradeclaw/platform/service.py` - convert instance lifecycle and recovery paths to async persistence-backed behavior
- `tradeclaw/runtime/scheduler.py` - add async error persistence callback for failed workers
- `tradeclaw/bootstrap.py` - async runtime construction, migration execution, repository injection, persisted recovery
- `tradeclaw/api/app.py` - await async service and approval methods
- `tradeclaw/api/runtime_loop.py` - await async approval expiration
- `tradeclaw/api/server.py` - async bootstrap on startup and async close on shutdown
- `tests/test_config.py` - cover database config parsing
- `tests/test_trace_store.py` - adapt trace-store tests to async storage semantics
- `tests/test_approval_queue.py` - adapt approval tests to async repository-backed gate
- `tests/test_platform_service.py` - adapt service tests to async persistence and recovery
- `tests/test_bootstrap.py` - cover async runtime bootstrap and restart recovery
- `tests/test_runtime_loop.py` - await async expiration path
- `tests/test_api_app.py` - verify API against async service and approval interfaces
- `tests/test_worker_trace.py` - verify worker writes durable trace events through the async trace store

### Verification

- `uv sync`
- `uv run python -m unittest tests.test_config tests.test_persistence tests.test_trace_store tests.test_approval_queue tests.test_platform_service tests.test_bootstrap tests.test_runtime_loop tests.test_api_app tests.test_worker_trace`

### Session Git Constraint

- Do not create a git commit unless the user explicitly requests one in this session.
- Use passing verification output as the execution checkpoint.

## Task 1: Add Async Storage Dependencies and Config Parsing

**Files:**
- Modify: `pyproject.toml`
- Modify: `tradeclaw/default_config.yaml`
- Modify: `tradeclaw/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing config tests**

Extend `tests/test_config.py` with database expectations:

```python
    def test_database_defaults_available(self):
        cfg = load_config(resolve_config_path())
        self.assertEqual(cfg.database.url, "sqlite+aiosqlite:///./tradeclaw.db")
        self.assertFalse(cfg.database.echo)
        self.assertTrue(cfg.database.pool_pre_ping)

    def test_database_override_is_loaded(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
database:
  url: postgresql+asyncpg://user:pass@localhost:5432/tradeclaw
  echo: true
  pool_pre_ping: false
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.database.url,
                "postgresql+asyncpg://user:pass@localhost:5432/tradeclaw",
            )
            self.assertTrue(cfg.database.echo)
            self.assertFalse(cfg.database.pool_pre_ping)
        finally:
            path.unlink(missing_ok=True)
```

- [ ] **Step 2: Run the focused config test command to verify RED**

Run: `uv run python -m unittest tests.test_config`

Expected: FAIL with `AttributeError: 'AppConfig' object has no attribute 'database'`.

- [ ] **Step 3: Add dependencies and config parsing**

Update `pyproject.toml` dependencies with:

```toml
    "sqlalchemy>=2.0.39",
    "alembic>=1.15.2",
    "aiosqlite>=0.21.0",
```

Add the default config block to `tradeclaw/default_config.yaml`:

```yaml
database:
  url: "sqlite+aiosqlite:///./tradeclaw.db"
  echo: false
  pool_pre_ping: true
```

Add database dataclasses and parsing to `tradeclaw/config.py`:

```python
@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    echo: bool
    pool_pre_ping: bool


@dataclass(frozen=True)
class AppConfig:
    server: ServerSettings
    data: DataSettings
    risk: RiskSettings
    approval: ApprovalSettings
    observability: ObservabilitySettings
    model: ModelSettings
    database: DatabaseSettings
```

```python
    database = data.get("database", {})
```

```python
        database=DatabaseSettings(
            url=str(database.get("url", "sqlite+aiosqlite:///./tradeclaw.db")).strip(),
            echo=bool(database.get("echo", False)),
            pool_pre_ping=bool(database.get("pool_pre_ping", True)),
        ),
```

- [ ] **Step 4: Re-run the focused config test command**

Run: `uv sync && uv run python -m unittest tests.test_config`

Expected: PASS.

## Task 2: Build the Async Persistence Foundation

**Files:**
- Create: `tradeclaw/persistence/errors.py`
- Create: `tradeclaw/persistence/db.py`
- Create: `tradeclaw/persistence/models.py`
- Create: `tradeclaw/persistence/repositories.py`
- Modify: `tradeclaw/persistence/__init__.py`
- Create: `tests/test_persistence.py`

- [ ] **Step 1: Write failing repository tests**

Create `tests/test_persistence.py` with the first failing repository expectations:

```python
import tempfile
import unittest
from pathlib import Path

from tradeclaw.persistence.db import create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.models import Base
from tradeclaw.persistence.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyInstanceRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTraceEventRepository,
)


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
```

- [ ] **Step 2: Run the focused repository test command to verify RED**

Run: `uv run python -m unittest tests.test_persistence`

Expected: FAIL with `ModuleNotFoundError` because the async persistence modules do not exist yet.

- [ ] **Step 3: Implement the minimal async persistence foundation**

Create `tradeclaw/persistence/errors.py`:

```python
class PersistenceError(Exception):
    pass


class RecordNotFoundError(PersistenceError):
    pass


class StateConflictError(PersistenceError):
    pass
```

Create `tradeclaw/persistence/db.py`:

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine_and_session_factory(url: str, echo: bool = False, pool_pre_ping: bool = True):
    engine = create_async_engine(url, echo=echo, pool_pre_ping=pool_pre_ping)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


async def dispose_engine(engine: AsyncEngine | None):
    if engine is not None:
        await engine.dispose()
```

Create `tradeclaw/persistence/models.py`:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tradeclaw.persistence.db import Base


class InstanceRecord(Base):
    __tablename__ = "instances"

    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    orchestrator_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TraceEventRecord(Base):
    __tablename__ = "trace_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_trace_events_run_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SystemStateRecord(Base):
    __tablename__ = "system_state"

    state_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
```

Create `tradeclaw/persistence/repositories.py` with explicit async repository methods:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update

from tradeclaw.persistence.errors import RecordNotFoundError, StateConflictError
from tradeclaw.persistence.models import (
    ApprovalRecord,
    InstanceRecord,
    SystemStateRecord,
    TraceEventRecord,
)


@dataclass(frozen=True)
class InstanceSnapshot:
    instance_id: str
    name: str
    template_id: str
    mode: str
    orchestrator_mode: str
    description: str
    data_provider: str | None
    status: str
    last_error: str
```

```python
class SqlAlchemyInstanceRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_instance(self, **kwargs) -> InstanceSnapshot:
        async with self.session_factory() as session:
            record = InstanceRecord(**kwargs)
            session.add(record)
            await session.commit()
            return InstanceSnapshot(
                instance_id=record.instance_id,
                name=record.name,
                template_id=record.template_id,
                mode=record.mode,
                orchestrator_mode=record.orchestrator_mode,
                description=record.description,
                data_provider=record.data_provider,
                status=record.status,
                last_error=record.last_error,
            )

    async def update_status(self, instance_id: str, status: str, last_error: str) -> InstanceSnapshot:
        async with self.session_factory() as session:
            result = await session.execute(
                update(InstanceRecord)
                .where(InstanceRecord.instance_id == instance_id)
                .values(status=status, last_error=last_error, updated_at=datetime.utcnow())
                .returning(InstanceRecord)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise RecordNotFoundError(instance_id)
            await session.commit()
            return InstanceSnapshot(
                instance_id=record.instance_id,
                name=record.name,
                template_id=record.template_id,
                mode=record.mode,
                orchestrator_mode=record.orchestrator_mode,
                description=record.description,
                data_provider=record.data_provider,
                status=record.status,
                last_error=record.last_error,
            )

    async def list_instances(self) -> list[InstanceSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(select(InstanceRecord).order_by(InstanceRecord.created_at.asc()))
            return [
                InstanceSnapshot(
                    instance_id=record.instance_id,
                    name=record.name,
                    template_id=record.template_id,
                    mode=record.mode,
                    orchestrator_mode=record.orchestrator_mode,
                    description=record.description,
                    data_provider=record.data_provider,
                    status=record.status,
                    last_error=record.last_error,
                )
                for record in result.scalars()
            ]

    async def get_instance(self, identifier: str) -> InstanceSnapshot:
        async with self.session_factory() as session:
            result = await session.execute(
                select(InstanceRecord).where(
                    (InstanceRecord.instance_id == identifier) | (InstanceRecord.name == identifier)
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise RecordNotFoundError(identifier)
            return InstanceSnapshot(
                instance_id=record.instance_id,
                name=record.name,
                template_id=record.template_id,
                mode=record.mode,
                orchestrator_mode=record.orchestrator_mode,
                description=record.description,
                data_provider=record.data_provider,
                status=record.status,
                last_error=record.last_error,
            )
```

```python
class SqlAlchemyApprovalRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_pending(self, approval_id: str, intent_id: str, mode: str, created_at: datetime, expires_at: datetime):
        async with self.session_factory() as session:
            record = ApprovalRecord(
                approval_id=approval_id,
                intent_id=intent_id,
                mode=mode,
                status="pending",
                reason="",
                created_at=created_at,
                expires_at=expires_at,
                resolved_at=None,
            )
            session.add(record)
            await session.commit()
            return record

    async def list_pending(self):
        async with self.session_factory() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.status == "pending")
                .order_by(ApprovalRecord.created_at.asc())
            )
            return list(result.scalars())

    async def resolve(self, approval_id: str, status: str, reason: str = ""):
        async with self.session_factory() as session:
            result = await session.execute(
                update(ApprovalRecord)
                .where(ApprovalRecord.approval_id == approval_id, ApprovalRecord.status == "pending")
                .values(status=status, reason=reason, resolved_at=datetime.utcnow())
                .returning(ApprovalRecord)
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise StateConflictError(approval_id)
            await session.commit()
            return record

    async def expire_pending(self, now: datetime):
        async with self.session_factory() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.status == "pending", ApprovalRecord.expires_at <= now)
                .order_by(ApprovalRecord.created_at.asc())
            )
            records = list(result.scalars())
            for record in records:
                record.status = "expired"
                record.reason = "approval timeout"
                record.resolved_at = now
            await session.commit()
            return records


class SqlAlchemyTraceEventRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def append_event(self, run_id: str, phase: str, payload: dict):
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.max(TraceEventRecord.sequence)).where(TraceEventRecord.run_id == run_id)
            )
            sequence = (result.scalar() or 0) + 1
            record = TraceEventRecord(run_id=run_id, sequence=sequence, phase=phase, payload=payload)
            session.add(record)
            await session.commit()
            return record

    async def list_run_events(self, run_id: str):
        async with self.session_factory() as session:
            result = await session.execute(
                select(TraceEventRecord)
                .where(TraceEventRecord.run_id == run_id)
                .order_by(TraceEventRecord.sequence.asc())
            )
            return list(result.scalars())


class SqlAlchemySystemStateRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def get_kill_switch_enabled(self) -> bool:
        async with self.session_factory() as session:
            record = await session.get(SystemStateRecord, "global")
            return False if record is None else bool(record.kill_switch_enabled)

    async def set_kill_switch_enabled(self, enabled: bool) -> bool:
        async with self.session_factory() as session:
            record = await session.get(SystemStateRecord, "global")
            if record is None:
                record = SystemStateRecord(state_key="global", kill_switch_enabled=enabled)
                session.add(record)
            else:
                record.kill_switch_enabled = enabled
                record.updated_at = datetime.utcnow()
            await session.commit()
            return bool(record.kill_switch_enabled)
```

- [ ] **Step 4: Re-run the focused repository test command**

Run: `uv run python -m unittest tests.test_persistence`

Expected: PASS for the initial repository coverage.

## Task 3: Add Alembic and Verify Schema Creation

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/20260403_01_unified_async_storage.py`
- Modify: `tests/test_persistence.py`

- [ ] **Step 1: Add a failing migration smoke test**

Extend `tests/test_persistence.py` with:

```python
    async def test_metadata_contains_runtime_tables(self):
        table_names = set(Base.metadata.tables)
        self.assertEqual(
            table_names,
            {"instances", "approvals", "trace_events", "system_state"},
        )
```

- [ ] **Step 2: Run the repository test command to verify RED against missing Alembic files**

Run: `uv run alembic -x db_url=sqlite:///./.tmp-plan-check.db upgrade head`

Expected: FAIL with `No such file or directory: 'alembic.ini'` or an Alembic configuration error because migration files do not exist yet.

- [ ] **Step 3: Add Alembic configuration and the initial migration**

Create `alembic/env.py` with metadata wiring:

```python
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from tradeclaw.persistence.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
```

Create `alembic.ini` with:

```ini
[alembic]
script_location = alembic
sqlalchemy.url = sqlite:///./tradeclaw.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

Add the async/offline migration runners in the same file:

```python
def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = context.get_x_argument(as_dictionary=True).get("db_url") or config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_args:
        configuration["sqlalchemy.url"] = x_args["db_url"]
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async def run_async_migrations():
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()

    import asyncio
    asyncio.run(run_async_migrations())
```

Create `alembic/versions/20260403_01_unified_async_storage.py` with table creation for:

```python
def upgrade() -> None:
    op.create_table(
        "instances",
        sa.Column("instance_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("orchestrator_mode", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("data_provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=64), primary_key=True),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "trace_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_trace_events_run_sequence"),
    )
    op.create_table(
        "system_state",
        sa.Column("state_key", sa.String(length=32), primary_key=True),
        sa.Column("kill_switch_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
```

Include the missing indexes explicitly:

```python
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_expires_at", "approvals", ["expires_at"])
    op.create_index("ix_approvals_status_expires_at", "approvals", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_approvals_status_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_table("system_state")
    op.drop_table("trace_events")
    op.drop_table("approvals")
    op.drop_table("instances")
```

- [ ] **Step 4: Run Alembic upgrade on a temporary SQLite database**

Run: `uv run alembic -x db_url=sqlite:///./.tmp-plan-check.db upgrade head`

Expected: PASS with all four tables created.

## Task 4: Replace the Trace Store With Async Persistence

**Files:**
- Modify: `tradeclaw/persistence/trace_store.py`
- Create: `tradeclaw/persistence/runtime_state.py`
- Modify: `tradeclaw/core/worker.py`
- Modify: `tests/test_trace_store.py`
- Modify: `tests/test_worker_trace.py`

- [ ] **Step 1: Write failing async trace-store tests**

Change `tests/test_trace_store.py` to `IsolatedAsyncioTestCase` and assert:

```python
class TraceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_only_and_query_by_run(self):
        store = AsyncTraceStore(trace_repository)

        await store.append(run_id="run-1", phase="load_context", payload={"ok": True})
        await store.append(run_id="run-1", phase="dispatch_orders", payload={"count": 1})
        await store.append(run_id="run-2", phase="load_context", payload={"ok": True})

        run1_events = await store.get_run_events("run-1")

        self.assertEqual(len(run1_events), 2)
        self.assertEqual(run1_events[0].sequence, 1)
        self.assertEqual(run1_events[1].sequence, 2)
```

Update `tests/test_worker_trace.py` to await async trace reads:

```python
        events = await store.get_run_events(worker.last_run_id)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[-1].phase, "persist_trace_and_metrics")
```

- [ ] **Step 2: Run the focused trace-store tests to verify RED**

Run: `uv run python -m unittest tests.test_trace_store tests.test_worker_trace`

Expected: FAIL because `append` and `get_run_events` are still synchronous.

- [ ] **Step 3: Implement the async trace store and worker integration**

Update `tradeclaw/persistence/trace_store.py` to keep `TraceEvent` and `InMemoryTraceStore` as a test double, and add:

```python
class AsyncTraceStore:
    def __init__(self, repository):
        self.repository = repository

    async def append(self, run_id: str, phase: str, payload: dict):
        return await self.repository.append_event(run_id=run_id, phase=phase, payload=payload)

    async def get_run_events(self, run_id: str):
        return await self.repository.list_run_events(run_id)
```

Update `tradeclaw/core/worker.py` to await trace writes:

```python
    async def _append_trace(self, run_id: str, phase: str, payload: dict):
        if self.trace_store is None:
            return
        await _maybe_await(self.trace_store.append(run_id=run_id, phase=phase, payload=payload))

    async def _record_phase(self, run_id: str, phase: str, payload: dict):
        await self._append_trace(run_id, phase, payload)
```

Also update every `self._record_phase(...)` call inside `run_cycle()` to `await self._record_phase(...)`.

- [ ] **Step 4: Re-run the focused trace-store tests**

Run: `uv run python -m unittest tests.test_trace_store tests.test_worker_trace`

Expected: PASS.

## Task 5: Convert Approval Gate to Async Repository-Backed State

**Files:**
- Modify: `tradeclaw/execution/approval.py`
- Modify: `tests/test_approval_queue.py`
- Modify: `tests/test_runtime_loop.py`

- [ ] **Step 1: Write failing async approval tests**

Convert `tests/test_approval_queue.py` to async tests and keep the existing behavior assertions, but await the gate:

```python
class ApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_order_enters_pending_queue(self):
        gate = QueuedApprovalGate(
            approval_repository=approval_repository,
            min_notional_for_approval=500.0,
            timeout_seconds=60,
        )

        result = await gate.request(self._intent(), mode="live")

        self.assertEqual(result.status, "pending")
        self.assertIsNotNone(result.approval_id)
        self.assertEqual(len(await gate.list_pending()), 1)
```

Update `tests/test_runtime_loop.py` fake gate to async:

```python
class _FakeApprovalGate:
    def __init__(self):
        self.calls = 0

    async def expire_pending(self):
        self.calls += 1
        return []
```

- [ ] **Step 2: Run the focused approval tests to verify RED**

Run: `uv run python -m unittest tests.test_approval_queue tests.test_runtime_loop`

Expected: FAIL because `QueuedApprovalGate` and `RuntimeTickLoop` are not fully async yet.

- [ ] **Step 3: Implement the async approval gate**

Update `tradeclaw/execution/approval.py` so `QueuedApprovalGate` accepts an async repository and exposes async methods:

```python
class QueuedApprovalGate:
    def __init__(self, approval_repository, require_approval_modes=None, min_notional_for_approval=0.0, timeout_seconds=300, clock=None):
        self.approval_repository = approval_repository
        self.require_approval_modes = set(require_approval_modes or {"live"})
        self.min_notional_for_approval = float(min_notional_for_approval)
        self.timeout_seconds = int(timeout_seconds)
        self.clock = clock or datetime.utcnow

    async def request(self, intent, account_snapshot=None, market_context=None, mode="paper") -> ApprovalResult:
        notional = _calculate_notional(intent)
        if mode not in self.require_approval_modes or notional < self.min_notional_for_approval:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)

        now = self.clock()
        approval_id = str(uuid.uuid4())
        await self.approval_repository.create_pending(
            approval_id=approval_id,
            intent_id=intent.intent_id,
            mode=mode,
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout_seconds),
        )
        return ApprovalResult(status="pending", intent_id=intent.intent_id, approval_id=approval_id)
```

Update `tradeclaw/api/runtime_loop.py`:

```python
                        expired = []
                        if hasattr(self.approval_gate, "expire_pending"):
                            expired = await self.approval_gate.expire_pending()
```

Also implement the remaining async gate methods:

```python
    async def list_pending(self):
        return await self.approval_repository.list_pending()

    async def approve(self, approval_id: str) -> ApprovalResult:
        pending = await self.approval_repository.resolve(approval_id, status="approved")
        return ApprovalResult(status="approved", intent_id=pending.intent_id, approval_id=pending.approval_id)

    async def reject(self, approval_id: str, reason: str = "") -> ApprovalResult:
        pending = await self.approval_repository.resolve(approval_id, status="rejected", reason=reason)
        return ApprovalResult(status="rejected", intent_id=pending.intent_id, reason=reason, approval_id=pending.approval_id)

    async def expire_pending(self, now: datetime | None = None):
        return await self.approval_repository.expire_pending(now or self.clock())
```

- [ ] **Step 4: Re-run the focused approval tests**

Run: `uv run python -m unittest tests.test_approval_queue tests.test_runtime_loop`

Expected: PASS.

## Task 6: Convert Platform Service and Scheduler to Async Persistent State

**Files:**
- Modify: `tradeclaw/platform/service.py`
- Modify: `tradeclaw/runtime/scheduler.py`
- Modify: `tests/test_platform_service.py`

- [ ] **Step 1: Write failing async platform-service tests**

Convert `tests/test_platform_service.py` to await service methods:

```python
        instance = await service.create_instance(name="alpha", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)
        await service.tick_once()

        status = await service.get_instance_status(instance.instance_id)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["cycles"], 1)
```

Add a recovery test:

```python
    async def test_recover_running_instances_from_repository(self):
        await service.create_instance(name="alpha", template_id="single-agent-trend")
        await service.start_instance("alpha")

        recovered = await service.restore_instances()

        self.assertEqual(recovered, 1)
        status = await service.get_instance_status("alpha")
        self.assertEqual(status["status"], "running")
```

Add a kill-switch persistence test:

```python
    async def test_kill_switch_blocks_restore_of_running_instances(self):
        await service.system_state_repository.set_kill_switch_enabled(True)
        instance = await service.create_instance(name="beta", template_id="single-agent-trend")
        await service.instance_repository.update_status(instance.instance_id, "running", "")

        recovered = await service.restore_instances()

        self.assertEqual(recovered, 0)
        status = await service.get_instance_status("beta")
        self.assertEqual(status["status"], "running")
```

Add an error-persistence test:

```python
    async def test_restore_failure_marks_instance_error(self):
        instance = await service.create_instance(name="gamma", template_id="single-agent-trend")
        await service.instance_repository.update_status(instance.instance_id, "running", "")
        service.worker_factory = lambda config: (_ for _ in ()).throw(RuntimeError("restore failed"))

        recovered = await service.restore_instances()

        self.assertEqual(recovered, 0)
        status = await service.get_instance_status("gamma")
        self.assertEqual(status["status"], "error")
        self.assertIn("restore failed", status["last_error"])
```

- [ ] **Step 2: Run the focused platform-service tests to verify RED**

Run: `uv run python -m unittest tests.test_platform_service`

Expected: FAIL because service methods are still synchronous and in-memory.

- [ ] **Step 3: Implement async instance persistence and scheduler error sync**

Update `tradeclaw/platform/service.py` constructor to accept repositories and keep only an in-memory worker cache:

```python
class TradingPlatformService:
    def __init__(
        self,
        scheduler,
        worker_factory,
        instance_repository,
        system_state_repository,
        templates=None,
        default_data_provider="auto",
    ):
        self.scheduler = scheduler
        self.worker_factory = worker_factory
        self.instance_repository = instance_repository
        self.system_state_repository = system_state_repository
        self.templates = templates or DEFAULT_TEMPLATES
        self.default_data_provider = (default_data_provider or "auto").strip().lower() or "auto"
        self.instances: Dict[str, AgentInstance] = {}
        self.kill_switch_enabled = False
```

Implement async instance methods:

```python
    async def create_instance(
        self,
        name: str,
        template_id: str,
        mode: str | None = None,
        orchestrator_mode: str | None = None,
        description: str = "",
        data_provider: str | None = None,
    ):
        template = self.templates[template_id]
        config = AgentInstanceConfig(
            name=name,
            mode=mode or template.default_mode,
            orchestrator_mode=orchestrator_mode or template.default_orchestrator_mode,
            template_id=template_id,
            description=description,
            data_provider=data_provider,
        )
        worker = self.worker_factory(config)
        record = await self.instance_repository.create_instance(
            instance_id=str(uuid.uuid4()),
            name=name,
            template_id=template_id,
            mode=config.mode,
            orchestrator_mode=config.orchestrator_mode,
            description=description,
            data_provider=data_provider,
            status="configured",
            last_error="",
        )
        instance = AgentInstance(instance_id=record.instance_id, config=config, worker=worker)
        self.instances[instance.instance_id] = instance
        self.scheduler.register(instance)
        return instance

    async def start_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        if self.kill_switch_enabled:
            raise RuntimeError("kill switch enabled")
        instance = await self._load_or_build_instance(record)
        self.scheduler.start(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "running", "")
        return instance

    async def pause_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = await self._load_or_build_instance(record)
        self.scheduler.pause(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "paused", "")
        return instance

    async def stop_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = await self._load_or_build_instance(record)
        self.scheduler.stop(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "stopped", "")
        return instance
```

Add the restore and kill-switch methods:

```python
    async def _load_or_build_instance(self, record):
        cached = self.instances.get(record.instance_id)
        if cached is not None:
            return cached
        config = AgentInstanceConfig(
            name=record.name,
            mode=record.mode,
            orchestrator_mode=record.orchestrator_mode,
            template_id=record.template_id,
            description=record.description,
            data_provider=record.data_provider,
        )
        worker = self.worker_factory(config)
        instance = AgentInstance(
            instance_id=record.instance_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )
        self.instances[instance.instance_id] = instance
        self.scheduler.register(instance)
        return instance

    async def restore_instances(self) -> int:
        restored = 0
        kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        self.kill_switch_enabled = kill_switch_enabled
        for record in await self.instance_repository.list_instances():
            try:
                instance = await self._load_or_build_instance(record)
            except Exception as exc:
                await self.instance_repository.update_status(record.instance_id, "error", str(exc))
                continue
            if record.status == "running" and not kill_switch_enabled:
                try:
                    self.scheduler.start(instance.instance_id)
                    restored += 1
                except Exception as exc:
                    await self.instance_repository.update_status(instance.instance_id, "error", str(exc))
        return restored

    async def set_kill_switch(self, enabled: bool):
        await self.system_state_repository.set_kill_switch_enabled(enabled)
        self.kill_switch_enabled = enabled
        if enabled:
            for instance in self.instances.values():
                if instance.status == "running":
                    self.scheduler.stop(instance.instance_id)

    async def get_instance_status(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = self.instances.get(record.instance_id)
        cycles = getattr(instance.worker, "cycles", None) if instance is not None else None
        effective = resolve_effective_provider(record.data_provider, self.default_data_provider)
        return {
            "instance_id": record.instance_id,
            "name": record.name,
            "mode": record.mode,
            "status": record.status,
            "cycles": cycles,
            "last_error": record.last_error,
            "data_provider": record.data_provider,
            "data_provider_effective": effective,
        }

    async def list_instances(self):
        records = await self.instance_repository.list_instances()
        return [await self.get_instance_status(record.instance_id) for record in records]

    async def get_system_state(self):
        kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        records = await self.instance_repository.list_instances()
        running_count = len([record for record in records if record.status == "running"])
        return {
            "kill_switch_enabled": kill_switch_enabled,
            "instance_count": len(records),
            "running_count": running_count,
        }
```

Update `tradeclaw/runtime/scheduler.py` to accept an async error callback:

```python
class RuntimeScheduler:
    def __init__(self, on_instance_error=None):
        self.instances: Dict[str, object] = {}
        self.on_instance_error = on_instance_error
```

```python
            except Exception as exc:
                instance.status = "error"
                instance.last_error = str(exc)
                if self.on_instance_error is not None:
                    await self.on_instance_error(instance.instance_id, str(exc))
```

- [ ] **Step 4: Re-run the focused platform-service tests**

Run: `uv run python -m unittest tests.test_platform_service`

Expected: PASS.

## Task 7: Make Bootstrap and API Startup Recover Persistent Runtime State

**Files:**
- Modify: `tradeclaw/bootstrap.py`
- Modify: `tradeclaw/api/server.py`
- Modify: `tradeclaw/api/app.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `tests/test_api_app.py`

- [ ] **Step 1: Write failing bootstrap and API recovery tests**

Update `tests/test_bootstrap.py` to await async runtime construction:

```python
            runtime = await build_platform_runtime(app_cfg=cfg)
            service = runtime["service"]

            instance = await service.create_instance(name="demo", template_id="single-agent-trend")
            await service.start_instance(instance.instance_id)
            executed = await service.tick_once()

            self.assertEqual(executed, 1)
            status = await service.get_instance_status(instance.instance_id)
            self.assertEqual(status["status"], "running")
```

Add restart recovery coverage:

```python
    async def test_runtime_restores_running_instances_on_rebuild(self):
        cfg = load_config(path)
        runtime = await build_platform_runtime(app_cfg=cfg)
        service = runtime["service"]
        instance = await service.create_instance(name="demo", template_id="single-agent-trend")
        await service.start_instance(instance.instance_id)
        await service.aclose()

        rebuilt = await build_platform_runtime(app_cfg=cfg)
        rebuilt_service = rebuilt["service"]
        status = await rebuilt_service.get_instance_status("demo")
        self.assertEqual(status["status"], "running")
```

Update `tests/test_api_app.py` fake service methods to async:

```python
    async def list_instances(self):
        return []

    async def get_system_state(self):
        return {"kill_switch_enabled": False, "instance_count": 0, "running_count": 0}
```

- [ ] **Step 2: Run the focused bootstrap and API tests to verify RED**

Run: `uv run python -m unittest tests.test_bootstrap tests.test_api_app`

Expected: FAIL because `build_platform_runtime()` and API handlers are not yet aligned to async persistence.

- [ ] **Step 3: Implement async bootstrap and persisted recovery**

Update `tradeclaw/bootstrap.py` to:

```python
async def build_platform_runtime(app_cfg: AppConfig | None = None):
    cfg = app_cfg or get_config()
    initialize_observability(
        service_name=cfg.observability.service_name,
        log_level=cfg.observability.log_level,
        tracing_enabled=cfg.observability.tracing_enabled,
        console_enabled=cfg.observability.console_enabled,
    )
    engine, session_factory = create_engine_and_session_factory(
        cfg.database.url,
        echo=cfg.database.echo,
        pool_pre_ping=cfg.database.pool_pre_ping,
    )
```

Run migrations before building repositories:

```python
    await run_migrations(cfg.database.url)
```

Add `run_migrations()` to `tradeclaw/persistence/runtime_state.py`:

```python
from __future__ import annotations

from alembic import command
from alembic.config import Config


async def run_migrations(db_url: str):
    import asyncio

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    await asyncio.to_thread(command.upgrade, config, "head")
```

Inject repositories and async gate:

```python
    approval_repository = SqlAlchemyApprovalRepository(session_factory)
    instance_repository = SqlAlchemyInstanceRepository(session_factory)
    system_state_repository = SqlAlchemySystemStateRepository(session_factory)
    trace_repository = SqlAlchemyTraceEventRepository(session_factory)
    approval_gate = QueuedApprovalGate(
        approval_repository=approval_repository,
        min_notional_for_approval=cfg.approval.min_notional_for_approval,
        timeout_seconds=cfg.approval.timeout_seconds,
    )
```

After service creation:

```python
    await service.restore_instances()
```

Update `tradeclaw/api/app.py` to await async service methods:

```python
    @app.get("/instances")
    async def list_instances():
        return await service.list_instances()

    @app.get("/system/state")
    async def get_system_state():
        return await service.get_system_state()

    @app.post("/system/kill-switch")
    async def set_kill_switch(payload: dict):
        enabled = bool(payload.get("enabled", True))
        await service.set_kill_switch(enabled)
        return await service.get_system_state()
```

Update `tradeclaw/api/server.py` so startup awaits async runtime build before starting the loop or stores the ready runtime during application construction.

- [ ] **Step 4: Re-run the focused bootstrap and API tests**

Run: `uv run python -m unittest tests.test_bootstrap tests.test_api_app`

Expected: PASS.

## Task 8: Run the Full Focused Regression Suite

**Files:**
- Verify only

- [ ] **Step 1: Run the full async-storage regression suite**

Run: `uv run python -m unittest tests.test_config tests.test_persistence tests.test_trace_store tests.test_approval_queue tests.test_platform_service tests.test_bootstrap tests.test_runtime_loop tests.test_api_app tests.test_worker_trace`

Expected: PASS with the persistence, runtime, API, and trace regressions covered.

- [ ] **Step 2: Run a migration smoke command after tests**

Run: `uv run alembic upgrade head`

Expected: PASS against the configured SQLite database, creating or upgrading the runtime storage schema.
