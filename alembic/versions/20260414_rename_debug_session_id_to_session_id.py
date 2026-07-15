"""Rename debug_session_id columns to session_id (debug_sessions, events, spans, backtest_jobs).

Revision ID: 20260414_01
Revises: 20260413_01
Create Date: 2026-04-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260414_01"
down_revision = "20260413_01"
branch_labels = None
depends_on = None


def _upgrade_sqlite() -> None:
    op.execute(sa.text("ALTER TABLE debug_session_events RENAME COLUMN debug_session_id TO session_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_debug_session_spans_debug_session_id"))
    op.execute(sa.text("ALTER TABLE debug_session_spans RENAME COLUMN debug_session_id TO session_id"))
    op.execute(
        sa.text("CREATE INDEX IF NOT EXISTS ix_debug_session_spans_session_id ON debug_session_spans (session_id)")
    )
    op.execute(sa.text("ALTER TABLE debug_sessions RENAME COLUMN debug_session_id TO session_id"))
    op.execute(sa.text("ALTER TABLE backtest_jobs RENAME COLUMN debug_session_id TO session_id"))


def _downgrade_sqlite() -> None:
    op.execute(sa.text("ALTER TABLE backtest_jobs RENAME COLUMN session_id TO debug_session_id"))
    op.execute(sa.text("ALTER TABLE debug_sessions RENAME COLUMN session_id TO debug_session_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_debug_session_spans_session_id"))
    op.execute(sa.text("ALTER TABLE debug_session_spans RENAME COLUMN session_id TO debug_session_id"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_debug_session_spans_debug_session_id "
            "ON debug_session_spans (debug_session_id)"
        )
    )
    op.execute(sa.text("ALTER TABLE debug_session_events RENAME COLUMN session_id TO debug_session_id"))


def _upgrade_non_sqlite() -> None:
    op.drop_constraint(
        "uq_debug_session_events_sequence",
        "debug_session_events",
        type_="unique",
    )
    op.alter_column(
        "debug_session_events",
        "debug_session_id",
        new_column_name="session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_debug_session_events_sequence",
        "debug_session_events",
        ["session_id", "sequence"],
    )

    op.drop_index("ix_debug_session_spans_debug_session_id", table_name="debug_session_spans")
    op.alter_column(
        "debug_session_spans",
        "debug_session_id",
        new_column_name="session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_index(
        "ix_debug_session_spans_session_id",
        "debug_session_spans",
        ["session_id"],
        unique=False,
    )

    op.alter_column(
        "debug_sessions",
        "debug_session_id",
        new_column_name="session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )

    op.alter_column(
        "backtest_jobs",
        "debug_session_id",
        new_column_name="session_id",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )


def _downgrade_non_sqlite() -> None:
    op.alter_column(
        "backtest_jobs",
        "session_id",
        new_column_name="debug_session_id",
        existing_type=sa.String(length=64),
        existing_nullable=True,
    )

    op.alter_column(
        "debug_sessions",
        "session_id",
        new_column_name="debug_session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )

    op.drop_index("ix_debug_session_spans_session_id", table_name="debug_session_spans")
    op.alter_column(
        "debug_session_spans",
        "session_id",
        new_column_name="debug_session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_index(
        "ix_debug_session_spans_debug_session_id",
        "debug_session_spans",
        ["debug_session_id"],
        unique=False,
    )

    op.drop_constraint(
        "uq_debug_session_events_sequence",
        "debug_session_events",
        type_="unique",
    )
    op.alter_column(
        "debug_session_events",
        "session_id",
        new_column_name="debug_session_id",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_debug_session_events_sequence",
        "debug_session_events",
        ["debug_session_id", "sequence"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_non_sqlite()


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_non_sqlite()
