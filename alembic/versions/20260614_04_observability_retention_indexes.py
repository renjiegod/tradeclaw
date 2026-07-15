"""observability retention: age indexes for the TTL prune.

The retention TTL prune
(:func:`doyoutrade.persistence.observability_ttl_prune.prune_observability_rows`)
deletes rows older than the window from ``debug_session_events`` (by
``timestamp``), ``debug_session_spans`` (by ``start_time``) and
``debug_sessions`` (by ``created_at``). Without an index on those age columns
the daily ``DELETE ... WHERE <ts> < :cutoff`` is a full table scan on exactly
the tables that grow fastest. ``model_invocations.created_at`` is already indexed
(``ix_model_invocations_created_at``) and ``cycle_runs`` is never pruned, so
neither needs a new index here.

Idempotent: each index is created only when absent.

Revision ID: 20260614_04
Revises: 20260614_03
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260614_04"
down_revision = "20260614_03"
branch_labels = None
depends_on = None


# (index_name, table, columns)
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_debug_session_events_timestamp", "debug_session_events", ["timestamp"]),
    ("ix_debug_session_spans_start_time", "debug_session_spans", ["start_time"]),
    ("ix_debug_sessions_created_at", "debug_sessions", ["created_at"]),
)


def _existing_indexes(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, columns in _INDEXES:
        if name not in _existing_indexes(bind, table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _columns in _INDEXES:
        if name in _existing_indexes(bind, table):
            op.drop_index(name, table_name=table)
