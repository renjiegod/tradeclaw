"""cron_jobs tagged-union schedule + delete_after_run

Adds three columns to ``cron_jobs`` that let the API accept a
schedule as a tagged union (``cron`` vs ``at``) instead of overloading
``cron_expression`` for every shape. Motivation: the dominant LLM
intent is "fire in N seconds/minutes", and squeezing that through a
5-field cron string is the source of every TZ-drift / wrong-minute
mistake we keep getting bitten by (see 2026-05-24 incidents replayed
at session asst-3efe1be9e4ff).

Schema delta:
  * ``schedule_kind`` (string, default ``cron``) — tagged-union
    discriminator.
  * ``at_iso`` (string, nullable) — ISO-8601 instant with offset for
    ``schedule_kind="at"``; null otherwise. Carries the timezone
    explicitly so caller cannot mis-pair HH:MM with a different tz.
  * ``delete_after_run`` (bool, default false) — replaces the
    pattern-sniffing "calendar pin auto-disable" helper from the
    2026-05-24 PR. ``at`` jobs default to true at the API layer; the
    column itself defaults false for back-compat with recurring
    cron-kind rows.

Existing rows are left untouched: ``schedule_kind`` server-defaults
to ``cron`` (matching the legacy behaviour), ``at_iso`` stays NULL,
``delete_after_run`` stays false. No data backfill needed.

Revision ID: 20260524_01
Revises: 20260522_02
Create Date: 2026-05-24 02:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260524_01"
down_revision = "20260522_02"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cron_cols = _existing_columns(bind, "cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch_op:
        if "schedule_kind" not in cron_cols:
            batch_op.add_column(
                sa.Column(
                    "schedule_kind",
                    sa.String(length=16),
                    nullable=False,
                    server_default="cron",
                )
            )
        if "at_iso" not in cron_cols:
            batch_op.add_column(
                sa.Column("at_iso", sa.String(length=64), nullable=True)
            )
        if "delete_after_run" not in cron_cols:
            batch_op.add_column(
                sa.Column(
                    "delete_after_run",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    cron_cols = _existing_columns(bind, "cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch_op:
        if "delete_after_run" in cron_cols:
            batch_op.drop_column("delete_after_run")
        if "at_iso" in cron_cols:
            batch_op.drop_column("at_iso")
        if "schedule_kind" in cron_cols:
            batch_op.drop_column("schedule_kind")
