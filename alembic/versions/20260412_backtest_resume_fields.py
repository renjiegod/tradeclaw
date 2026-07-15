"""Backtest cross-restart: stop flag, ledger checkpoint, return basis.

Revision ID: 20260412_01
Revises: 20260411_01
Create Date: 2026-04-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260412_01"
down_revision = "20260411_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtest_jobs",
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "backtest_jobs",
        sa.Column("ledger_checkpoint_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "backtest_jobs",
        sa.Column("reference_starting_equity", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_jobs", "reference_starting_equity")
    op.drop_column("backtest_jobs", "ledger_checkpoint_json")
    op.drop_column("backtest_jobs", "stop_requested")
