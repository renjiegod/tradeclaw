"""cycle_runs: index trace_id.

Adds ``ix_cycle_runs_trace_id`` so the new trace-scoped debug view
(``GET /traces/{trace_id}/debug-view`` →
``SqlAlchemyCycleRunRepository.list_by_trace_id``) can resolve all cycle
runs carrying an OpenTelemetry trace_id without a full table scan. The
``debug_session_spans`` and ``model_invocations`` tables already index
trace_id; this brings ``cycle_runs`` in line.

Revision ID: 20260603_01
Revises: 20260601_01
Create Date: 2026-06-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260603_01"
down_revision = "20260601_01"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "ix_cycle_runs_trace_id" not in _existing_indexes(bind, "cycle_runs"):
        op.create_index("ix_cycle_runs_trace_id", "cycle_runs", ["trace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_cycle_runs_trace_id" in _existing_indexes(bind, "cycle_runs"):
        op.drop_index("ix_cycle_runs_trace_id", table_name="cycle_runs")
