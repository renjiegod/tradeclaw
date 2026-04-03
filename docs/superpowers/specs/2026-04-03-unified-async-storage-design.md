# Tradeclaw Unified Async Storage Design

## Goal

Introduce a unified asynchronous persistence layer based on SQLAlchemy so Tradeclaw can use SQLite by default while remaining easy to switch to MySQL or PostgreSQL later, and persist runtime state across process restarts for instances, approval queues, system flags, and trace events.

## Current State

The codebase currently mixes persistent concerns with in-memory state:

- `tradeclaw.persistence.trace_store.InMemoryTraceStore` stores trace events only in memory
- `tradeclaw.execution.approval.QueuedApprovalGate` stores pending approvals in an in-memory dictionary
- `tradeclaw.platform.service.TradingPlatformService` stores instances and runtime state in an in-memory dictionary
- startup does not restore runtime state from storage
- configuration has no database section and no migration workflow

This creates several problems:

- instance metadata and status are lost on restart
- pending approval records are lost on restart
- kill switch state is not durable
- trace events cannot be queried after process exit
- there is no storage boundary that can be reused when switching from SQLite to MySQL or PostgreSQL

## Chosen Approach

Use SQLAlchemy's async stack as the only database access path:

- `AsyncEngine` for connections
- `AsyncSession` and `async_sessionmaker` for units of work
- SQLAlchemy ORM models for schema mapping
- repository interfaces for business-facing storage access
- Alembic for schema migrations from the first version onward

The implementation will keep the database as the persistent source of truth and use in-process objects only for active execution:

- runtime instances, approval requests, trace events, and kill switch state are stored in database tables
- `TradingPlatformService` and `QueuedApprovalGate` move from in-memory dictionaries to async repository calls
- `RuntimeScheduler` still owns active worker objects in the current process, but not durable state
- startup restores persisted runtime state and automatically re-registers previously running instances when the kill switch is not enabled

This is preferred over a synchronous SQLAlchemy design because the API layer is already async and the user explicitly wants an all-async solution. It is preferred over a hybrid memory-plus-snapshot design because that would leave two competing state sources and make restart recovery less predictable.

## Non-Goals

This design does not include:

- a generic plugin system for arbitrary storage backends beyond SQL databases
- historical versioning for instance configuration changes
- multi-node leader election or distributed scheduler coordination
- trace analytics or replay tooling beyond durable append and query support
- automatic conversion of every domain object into an ORM model

These can be added later without changing the core storage boundary defined here.

## Configuration

Add a `database` section to application config.

Initial fields:

- `url`
- `echo`
- `pool_pre_ping`

Defaults:

- `url: sqlite+aiosqlite:///./tradeclaw.db`
- `echo: false`
- `pool_pre_ping: true`

Behavior:

- SQLite is the default local database and requires no external service
- switching to MySQL or PostgreSQL only requires changing `database.url` and installing a compatible async driver
- example future URLs:
  - `mysql+asyncmy://user:pass@host:3306/tradeclaw`
  - `postgresql+asyncpg://user:pass@host:5432/tradeclaw`

The config loader should parse the new `database` block into a dedicated settings dataclass and keep the rest of the existing config behavior unchanged.

## Module Design

### `tradeclaw.persistence.db`

Responsibilities:

- define the declarative base for ORM models
- create and cache the shared `AsyncEngine`
- expose `create_session_factory(...)`
- expose `get_session_factory(...)`
- expose `dispose_engine(...)` for clean shutdown in tests and runtime teardown

This module owns database connectivity but does not contain business queries.

### `tradeclaw.persistence.models`

Responsibilities:

- define ORM tables for durable runtime state
- centralize schema metadata used by Alembic

Initial models:

- `InstanceRecord`
- `ApprovalRecord`
- `TraceEventRecord`
- `SystemStateRecord`

Each model should stay focused on persistence mapping and avoid embedding business logic.

### `tradeclaw.persistence.repositories`

Responsibilities:

- define async repository interfaces that speak in domain terms
- hide ORM and query details from service and approval layers
- keep transaction scopes short and explicit

Initial repositories:

- `InstanceRepository`
- `ApprovalRepository`
- `TraceEventRepository`
- `SystemStateRepository`

Concrete SQLAlchemy implementations should live beside the interfaces or in a `sqlalchemy/` subpackage if the package grows later.

### `tradeclaw.persistence.bootstrap`

Responsibilities:

- initialize the async engine from config
- ensure migrations are applied before runtime services start
- build repository instances from the shared session factory

This creates one entrypoint for persistence setup so runtime bootstrap code does not need to know SQLAlchemy details.

## Database Schema

### `instances`

Purpose:

- persist instance configuration and current runtime status

Fields:

- `instance_id` primary key
- `name` unique, not null
- `template_id` not null
- `mode` not null
- `orchestrator_mode` not null
- `description` not null, default empty string
- `data_provider` nullable
- `status` not null
- `last_error` not null, default empty string
- `created_at` not null
- `updated_at` not null

Status values:

- `configured`
- `running`
- `paused`
- `stopped`
- `error`

### `approvals`

Purpose:

- persist approval queue entries and their resolution state

Fields:

- `approval_id` primary key
- `intent_id` not null
- `mode` not null
- `status` not null
- `reason` not null, default empty string
- `created_at` not null
- `expires_at` not null
- `resolved_at` nullable

Status values:

- `pending`
- `approved`
- `rejected`
- `expired`

Indexes:

- index on `status`
- index on `expires_at`
- compound index on `status, expires_at`

### `trace_events`

Purpose:

- persist append-only run trace events for audit and replay

Fields:

- `id` integer primary key
- `run_id` not null
- `sequence` not null
- `phase` not null
- `payload` not null
- `timestamp` not null

Constraints:

- unique constraint on `run_id, sequence`

Notes:

- `payload` should use a JSON-capable SQLAlchemy type so SQLite, MySQL, and PostgreSQL all receive a portable structure

### `system_state`

Purpose:

- persist global runtime flags that affect startup recovery

Fields:

- `state_key` primary key
- `kill_switch_enabled` not null
- `updated_at` not null

Notes:

- this table can start with a single row keyed as `global`
- using a keyed table instead of a hard-coded singleton row keeps the schema extensible

## Domain and Repository Boundaries

### Instances

`TradingPlatformService` should stop owning the durable instance map. Instead it should:

- request new instance creation through `InstanceRepository`
- resolve identifiers by querying repository data
- update status transitions through repository methods
- reconstruct `AgentInstance` objects from repository records when they need to exist in memory

The service may still keep an in-memory map of active `AgentInstance` objects that have registered workers, but that cache exists only to drive the local scheduler and can always be rebuilt from persistent state.

### Approvals

`QueuedApprovalGate` should stop owning a `_pending` dictionary. Instead it should:

- create pending approval rows when a request requires human approval
- list pending approvals by querying records with `status == "pending"`
- approve or reject only rows that are still pending
- expire overdue pending rows through an async bulk update path

The gate remains responsible for approval policy decisions such as notional threshold and required modes.

### Trace Events

The trace store interface should remain append-oriented, but the implementation should write to `TraceEventRepository` instead of a process-local list.

The repository should expose:

- append event
- list events for a run in ascending sequence order

## Startup and Recovery Flow

On runtime startup:

1. Load config, including the new `database` settings.
2. Build the async engine and session factory.
3. Run Alembic migrations to the latest revision before constructing runtime services.
4. Build repository instances and inject them into bootstrap, platform service, approval gate, and trace store.
5. Read `system_state` for the global kill switch flag.
6. Load all persisted instance records.
7. Reconstruct `AgentInstanceConfig` objects for every row and register corresponding `AgentInstance` objects with the scheduler.
8. For records whose status is `running`:
   - if the kill switch is disabled, rebuild the worker and move the in-memory instance into active scheduling
   - if the kill switch is enabled, keep the persisted row but do not auto-start the worker
9. Load pending approvals:
   - approvals whose `expires_at` is already in the past should be marked `expired`
   - approvals still within their window remain visible via the API

If worker reconstruction fails for a persisted running instance:

- update the record status to `error`
- persist the exception string in `last_error`
- continue recovering remaining instances

This ensures one broken instance does not block system startup.

## Runtime Behavior

### Instance Lifecycle

`create_instance` should:

- validate the requested template and provider choices
- persist the instance row first
- build the worker
- register the instance in the scheduler
- return the stored status view

If worker construction fails after persistence, the instance row should be updated to `error` with `last_error` populated.

`start_instance`, `pause_instance`, and `stop_instance` should:

- update the instance row status in the database
- keep the scheduler registration in sync after the database write succeeds

Database state remains the persistent truth, while the scheduler stays a process-local executor.

### Kill Switch

`set_kill_switch` should:

- persist the flag in `system_state`
- stop in-memory running instances when enabled
- prevent auto-start of persisted running instances on the next restart while the flag remains enabled

### Approval Expiration

Approval expiration should be handled through async repository operations that only transition rows currently in `pending` state. This prevents double-resolution when multiple API requests or background ticks race to process the same approval.

### Scheduler Error Sync

If a worker crashes during `RuntimeScheduler.tick_once()`:

- set the in-memory instance status to `error`
- persist `status="error"` and `last_error=<exception string>` asynchronously

Scheduler error handling should keep existing best-effort semantics while making the failure durable.

## Transaction and Concurrency Strategy

Each repository method should use a short-lived async session and commit or roll back within that method.

Design principles:

- do not hold one long-lived `AsyncSession` across requests or runtime ticks
- keep repository methods small and explicit
- use conditional updates for approval resolution and status transitions where races are possible
- treat a zero-row update as a business conflict, not as success

Approval resolution should use queries equivalent to:

- update where `approval_id = :id and status = 'pending'`

This ensures that once one caller approves or rejects an approval, another caller cannot silently overwrite it.

Trace sequence generation should remain simple in the first version:

- within an append transaction, fetch the current maximum `sequence` for the run and write the next value

This is acceptable for SQLite-first deployment and can be optimized later if a higher write rate appears.

## Alembic Strategy

Alembic should be included from the first version rather than introduced later.

Initial requirements:

- add Alembic to project dependencies
- add standard Alembic configuration and migration environment files
- point Alembic metadata discovery at the SQLAlchemy declarative base
- create an initial migration that creates `instances`, `approvals`, `trace_events`, and `system_state`

Runtime startup should fail clearly if the database cannot be migrated to the expected version.

The design intentionally avoids `metadata.create_all()` as the primary schema management path because the user wants migration-based evolution from the start.

## Error Handling

- database initialization failures should fail fast during bootstrap
- migration failures should abort startup before runtime services are created
- repository methods should translate missing rows or conflicting state transitions into domain-meaningful exceptions
- API handlers should continue mapping those exceptions to HTTP status codes
- recovery should be resilient: one failed instance becomes `error` and does not abort recovery of others

## Testing Strategy

Tests should use the async SQLAlchemy stack end to end with temporary SQLite databases.

### Config Tests

Verify:

- default database settings are available
- custom database URLs override defaults
- existing config sections still merge correctly with the new `database` block

### Repository Tests

Verify:

- instance creation, listing, lookup, and status updates
- approval creation, listing, approval, rejection, and expiration transitions
- trace event append and ordered readback per `run_id`
- system state read and write behavior

### Service Tests

Verify:

- instance creation persists metadata and registers a worker
- stopping and restarting the service restores persisted instances
- previously running instances auto-recover when kill switch is disabled
- previously running instances do not auto-recover when kill switch is enabled
- worker reconstruction failures persist `error` state

### API Tests

Verify:

- instance endpoints work through async persistence-backed services
- approval endpoints reflect durable pending and resolved state
- kill switch endpoint persists system state
- tick endpoint still expires approvals and reports counts correctly

### Migration Tests

Verify:

- the initial Alembic migration can upgrade an empty database
- upgraded schema contains the required tables and key constraints

## Implementation Notes

- start with repository interfaces shaped around current business operations rather than generic CRUD
- keep ORM models under `tradeclaw.persistence` and avoid leaking them into domain, API, or scheduler modules
- preserve current public behavior where possible so storage refactoring does not force unnecessary API changes
- prefer additive config changes so existing local configs continue to work with defaults

## Summary

Tradeclaw will adopt an async SQLAlchemy persistence layer with Alembic-managed schema migrations, defaulting to SQLite through an async driver. Durable storage will cover runtime instances, pending approvals, global system state, and trace events. Startup will restore persisted state and automatically recover previously running instances when allowed by the persisted kill switch. Service and approval logic will move to async repository-backed flows, while the scheduler remains responsible only for active in-process execution.
