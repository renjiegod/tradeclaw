"""Drop tasks.tick_mode (Phase 3 of Task-centric Trigger unification).

``tick_mode`` is obsolete: nothing auto-ticks anymore. Continuous trading is now
an ``interval``/``trade`` Task Trigger; the ``RuntimeTickLoop`` is retired and the
``TriggerScheduler`` is the sole driver.

Behavior-preserving: BEFORE dropping the column we SYNTHESIZE a continuity
``interval``/``trade`` trigger for every currently-``running`` task that was on
``tick_mode='interval'`` (idempotent — skip tasks that already own an interval
trigger). Those tasks keep firing cycles under the scheduler. ``next_fire_at`` is
left NULL so the scheduler lazy-inits it on the first scan (see
``TriggerScheduler._maybe_fire``).

Revision ID: 20260612_01
Revises: 20260611_02
Create Date: 2026-06-12 00:00:00.000000
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260612_01"
down_revision = "20260611_02"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def _has_check(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c.get("name") == name for c in insp.get_check_constraints(table))


def _synthesize_continuity_triggers() -> None:
    """Give every running interval task an interval/trade trigger so it keeps firing."""
    if not (
        _has_table("tasks")
        and _has_column("tasks", "tick_mode")
        and _has_table("task_triggers")
    ):
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT task_id FROM tasks WHERE tick_mode = 'interval' AND status = 'running'"
        )
    ).fetchall()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        task_id = row[0]
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM task_triggers "
                "WHERE task_id = :t AND schedule_kind = 'interval' LIMIT 1"
            ),
            {"t": task_id},
        ).fetchone()
        if already is not None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO task_triggers ("
                "id, task_id, name, enabled, status, schedule_kind, interval_seconds, "
                "timezone, delete_after_run, execution_intent, delivery_json, "
                "next_fire_at, last_error, created_at, updated_at"
                ") VALUES ("
                ":id, :task_id, :name, :enabled, :status, :schedule_kind, :interval_seconds, "
                ":timezone, :delete_after_run, :execution_intent, :delivery_json, "
                ":next_fire_at, :last_error, :created_at, :updated_at"
                ")"
            ),
            {
                "id": "trg-" + uuid.uuid4().hex[:12],
                "task_id": task_id,
                "name": "auto-interval (migrated)",
                "enabled": True,
                "status": "active",
                "schedule_kind": "interval",
                "interval_seconds": 5,
                "timezone": "UTC",
                "delete_after_run": False,
                "execution_intent": "trade",
                "delivery_json": None,
                "next_fire_at": None,
                "last_error": "",
                "created_at": now,
                "updated_at": now,
            },
        )


def upgrade() -> None:
    _synthesize_continuity_triggers()

    if not _has_column("tasks", "tick_mode"):
        return

    with op.batch_alter_table("tasks") as batch_op:
        if _has_check("tasks", "ck_tasks_tick_mode"):
            batch_op.drop_constraint("ck_tasks_tick_mode", type_="check")
        batch_op.drop_column("tick_mode")


def downgrade() -> None:
    if _has_column("tasks", "tick_mode"):
        return
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tick_mode",
                sa.String(length=32),
                nullable=False,
                server_default="interval",
            )
        )
        batch_op.create_check_constraint(
            "ck_tasks_tick_mode",
            "tick_mode IN ('interval', 'cron_driven')",
        )
