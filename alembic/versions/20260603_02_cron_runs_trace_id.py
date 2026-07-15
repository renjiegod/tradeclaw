"""cron_job_runs: add + index trace_id.

Captures the OpenTelemetry trace_id of the ``cron.job.fire`` span on each
cron firing so an operator who only has a trace_id (from a log line / span)
can reverse-resolve which cron run produced it. Backs
``GET /assistant/cron-job-runs?trace_id=...`` →
``SqlAlchemyCronJobRunRepository.list_by_trace_id`` and the
``doyoutrade-cli cron runs by-trace`` command.

Nullable: legacy rows pre-date the column and fires where tracing was a
no-op (all-zero / invalid trace id) intentionally leave it NULL.

Revision ID: 20260603_02
Revises: 20260603_01
Create Date: 2026-06-03 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260603_02"
down_revision = "20260603_01"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def _existing_indexes(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "cron_job_runs")
    if "trace_id" not in cols:
        op.add_column("cron_job_runs", sa.Column("trace_id", sa.String(64), nullable=True))
    if "ix_cron_job_runs_trace_id" not in _existing_indexes(bind, "cron_job_runs"):
        op.create_index("ix_cron_job_runs_trace_id", "cron_job_runs", ["trace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_cron_job_runs_trace_id" in _existing_indexes(bind, "cron_job_runs"):
        op.drop_index("ix_cron_job_runs_trace_id", table_name="cron_job_runs")
    if "trace_id" in _existing_columns(bind, "cron_job_runs"):
        op.drop_column("cron_job_runs", "trace_id")
