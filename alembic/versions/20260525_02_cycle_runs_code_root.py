"""Add code_version + code_hash to cycle_runs; add code_version to runs (backtest jobs).

The ``runs`` table already has ``strategy_code_hash`` (the hash of the strategy
definition at backtest-start); this migration adds the complementary
``code_version`` label (e.g. ``v0001-abc123ef``) so analytics can reconstruct
the exact on-disk path without joining against ``strategy_definitions``.

``cycle_runs`` gains both columns so the worker can pin the version it compiled
at cycle-start — protecting in-flight cycles from a concurrent assistant edit
that bumps ``current_version``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260525_02"
down_revision = "20260525_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cycle_runs") as batch:
        batch.add_column(sa.Column("code_version", sa.Text(), nullable=True))
        batch.add_column(sa.Column("code_hash", sa.Text(), nullable=True))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("code_version", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("code_version")
    with op.batch_alter_table("cycle_runs") as batch:
        batch.drop_column("code_hash")
        batch.drop_column("code_version")
