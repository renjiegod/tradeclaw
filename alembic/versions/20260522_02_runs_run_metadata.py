"""runs: add config_snapshot_json / engine_version / strategy_code_hash.

Three columns that snapshot the run's provenance so analytics can
reproduce or attribute results after the source task / definition has
been edited:

- ``config_snapshot_json`` — full effective CycleTaskConfig (parameters,
  position constraints, approval policy, etc.) at run start.
- ``engine_version`` — identifier of the worker / runner / compiler
  version that produced the run (e.g. ``"doyoutrade-0.4.1"``).
- ``strategy_code_hash`` — ``StrategyDefinition.code_hash`` captured at
  run start; lets analytics tie a run to the exact source it compiled
  even after the definition row has been updated.

All three are nullable: pre-migration ``runs`` rows pre-date the
snapshot fields and stay NULL. No backfill — historical analytics
should treat NULL as "metadata unknown (pre-0.4.1 runtime)".

Revision ID: 20260522_02
Revises: 20260522_01
Create Date: 2026-05-22 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260522_02"
down_revision = "20260522_01"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "runs")
    with op.batch_alter_table("runs") as batch_op:
        if "config_snapshot_json" not in cols:
            batch_op.add_column(sa.Column("config_snapshot_json", sa.JSON(), nullable=True))
        if "engine_version" not in cols:
            batch_op.add_column(sa.Column("engine_version", sa.String(length=64), nullable=True))
        if "strategy_code_hash" not in cols:
            batch_op.add_column(sa.Column("strategy_code_hash", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "runs")
    with op.batch_alter_table("runs") as batch_op:
        if "strategy_code_hash" in cols:
            batch_op.drop_column("strategy_code_hash")
        if "engine_version" in cols:
            batch_op.drop_column("engine_version")
        if "config_snapshot_json" in cols:
            batch_op.drop_column("config_snapshot_json")
