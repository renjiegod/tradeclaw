"""Drop cycle_runs.backtest_job_id; filter backtest cycles by session_id.

Revision ID: 20260415_01
Revises: 20260414_01
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_01"
down_revision = "20260414_01"
branch_labels = None
depends_on = None


def _upgrade_sqlite() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_cycle_runs_backtest_job"))
    op.execute(sa.text("ALTER TABLE cycle_runs DROP COLUMN backtest_job_id"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_cycle_runs_session_started "
            "ON cycle_runs (session_id, wall_started_at)"
        )
    )


def _downgrade_sqlite() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_cycle_runs_session_started"))
    op.execute(sa.text("ALTER TABLE cycle_runs ADD COLUMN backtest_job_id VARCHAR(64)"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_cycle_runs_backtest_job "
            "ON cycle_runs (backtest_job_id, wall_started_at)"
        )
    )


def _upgrade_non_sqlite() -> None:
    op.drop_index("ix_cycle_runs_backtest_job", table_name="cycle_runs")
    op.drop_column("cycle_runs", "backtest_job_id")
    op.create_index(
        "ix_cycle_runs_session_started",
        "cycle_runs",
        ["session_id", "wall_started_at"],
        unique=False,
    )


def _downgrade_non_sqlite() -> None:
    op.drop_index("ix_cycle_runs_session_started", table_name="cycle_runs")
    op.add_column(
        "cycle_runs",
        sa.Column("backtest_job_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_cycle_runs_backtest_job",
        "cycle_runs",
        ["backtest_job_id", "wall_started_at"],
        unique=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _upgrade_sqlite()
    else:
        _upgrade_non_sqlite()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_non_sqlite()
