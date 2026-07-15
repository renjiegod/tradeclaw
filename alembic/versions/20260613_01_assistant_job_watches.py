"""assistant_job_watches: background-job completion wake-ups.

A row registers "when job X reaches a terminal status, wake assistant
session Y" — written by the in-process ``watch_job`` tool, polled and
resolved by ``doyoutrade/assistant/job_watcher.py::JobWatchService``.

Revision ID: 20260613_01
Revises: 20260612_01
Create Date: 2026-06-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260613_01"
down_revision = "20260612_01"
branch_labels = None
depends_on = None

_TABLE = "assistant_job_watches"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("watch_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("assistant_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("job_kind", sa.String(32), nullable=False, server_default="backtest"),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_assistant_job_watches_status", _TABLE, ["status"])
    op.create_index("ix_assistant_job_watches_session_id", _TABLE, ["session_id"])
    op.create_index("ix_assistant_job_watches_job_id", _TABLE, ["job_id"])


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_assistant_job_watches_job_id", table_name=_TABLE)
    op.drop_index("ix_assistant_job_watches_session_id", table_name=_TABLE)
    op.drop_index("ix_assistant_job_watches_status", table_name=_TABLE)
    op.drop_table(_TABLE)
