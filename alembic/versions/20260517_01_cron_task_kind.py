"""cron_jobs task_kind/task_params_json + cron_job_runs delivery columns

Adds the polymorphic ``task_kind``/``task_params_json`` columns that let
``cron_manager`` dispatch fires through ``JobTaskRegistry`` instead of the
hard-coded "render template + send_message" tail. Legacy rows are
backfilled to ``task_kind='agent_chat_reply'`` so old data keeps working
through the same code path; ``input_template`` becomes nullable because
new task-based rows store everything in ``task_params_json``.

Also adds ``cron_task_kind``/``delivery_status`` to ``cron_job_runs`` so
per-fire history surfaces which kind ran and what happened to the user
push (delivered / suppressed / skipped / failed / none).

Revision ID: 20260517_01
Revises: 20260514_01
Create Date: 2026-05-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260517_01"
down_revision = "20260514_01"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def _column_is_nullable(bind, table: str, column: str) -> bool | None:
    insp = inspect(bind)
    if not insp.has_table(table):
        return None
    for col in insp.get_columns(table):
        if col["name"] == column:
            return bool(col.get("nullable", True))
    return None


def upgrade() -> None:
    bind = op.get_bind()

    cron_cols = _existing_columns(bind, "cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch_op:
        if "task_kind" not in cron_cols:
            batch_op.add_column(
                sa.Column("task_kind", sa.String(length=64), nullable=True)
            )
        if "task_params_json" not in cron_cols:
            batch_op.add_column(
                sa.Column("task_params_json", sa.JSON(), nullable=True)
            )
        # ``input_template`` is no longer required: task-based rows store the
        # user's request inside ``task_params_json``. We loosen the
        # constraint *after* backfill below so existing readers keep working
        # — see the data migration block.

    run_cols = _existing_columns(bind, "cron_job_runs")
    with op.batch_alter_table("cron_job_runs") as batch_op:
        if "cron_task_kind" not in run_cols:
            batch_op.add_column(
                sa.Column("cron_task_kind", sa.String(length=64), nullable=True)
            )
        if "delivery_status" not in run_cols:
            batch_op.add_column(
                sa.Column("delivery_status", sa.String(length=32), nullable=True)
            )

    # ── Data backfill: pure-text reminder rows → agent_chat_reply ───────────
    # Conservative: only rows *without* a ``pre_action`` are auto-migrated,
    # because those provably had no extra data-gathering step. Rows with a
    # ``pre_action`` (e.g. ``strategy_cycle``) keep ``task_kind=NULL`` and
    # continue through cron_manager's legacy pipeline; they can be migrated
    # by hand later once their replacement kind is exercised in production.
    rows = bind.execute(
        sa.text(
            "SELECT id, agent_id, input_template "
            "FROM cron_jobs "
            "WHERE task_kind IS NULL AND pre_action IS NULL"
        )
    ).fetchall()

    update_stmt = sa.text(
        "UPDATE cron_jobs SET task_kind = :kind, "
        "task_params_json = :params WHERE id = :id"
    ).bindparams(sa.bindparam("params", type_=sa.JSON()))

    for row in rows:
        params = {
            "user_request": row.input_template or "",
            "target_session_id": None,
            "agent_id": row.agent_id,
        }
        bind.execute(
            update_stmt,
            {"kind": "agent_chat_reply", "params": params, "id": row.id},
        )

    # Now safe to relax ``input_template`` NOT NULL — every row either had a
    # non-null value preserved as-is, or remains a legacy backfill anchor.
    if _column_is_nullable(bind, "cron_jobs", "input_template") is False:
        with op.batch_alter_table("cron_jobs") as batch_op:
            batch_op.alter_column(
                "input_template",
                existing_type=sa.Text(),
                nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()

    # Re-tighten input_template to NOT NULL — only safe if every row has a
    # value. Backfill rows we may have stamped with an empty string keep
    # their value; rows created post-migration without an input_template
    # need a placeholder to satisfy the constraint.
    if _column_is_nullable(bind, "cron_jobs", "input_template") is True:
        bind.execute(
            sa.text(
                "UPDATE cron_jobs SET input_template = '' "
                "WHERE input_template IS NULL"
            )
        )
        with op.batch_alter_table("cron_jobs") as batch_op:
            batch_op.alter_column(
                "input_template",
                existing_type=sa.Text(),
                nullable=False,
            )

    run_cols = _existing_columns(bind, "cron_job_runs")
    with op.batch_alter_table("cron_job_runs") as batch_op:
        if "delivery_status" in run_cols:
            batch_op.drop_column("delivery_status")
        if "cron_task_kind" in run_cols:
            batch_op.drop_column("cron_task_kind")

    cron_cols = _existing_columns(bind, "cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch_op:
        if "task_params_json" in cron_cols:
            batch_op.drop_column("task_params_json")
        if "task_kind" in cron_cols:
            batch_op.drop_column("task_kind")
