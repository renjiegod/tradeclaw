"""runs: add debug_enabled flag.

Records whether a run captured full debug observability (debug_sessions /
debug_session_spans / span events / cycle_runs / model_invocations) or executed
in fast mode (only run status + report + trade_fills). When False the absence of
trace detail is intentional, not a fault — surfaces let operators / run-view tell
the difference instead of treating an empty trace as broken.

NOT NULL with server_default TRUE: pre-migration ``runs`` rows pre-date the
toggle and were always full-debug, so True is the correct backfill value.

Revision ID: 20260527_02
Revises: 20260527_01
Create Date: 2026-05-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260527_02"
down_revision = "20260527_01"
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
        if "debug_enabled" not in cols:
            batch_op.add_column(
                sa.Column(
                    "debug_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
        if "config_overrides_json" not in cols:
            batch_op.add_column(sa.Column("config_overrides_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "runs")
    with op.batch_alter_table("runs") as batch_op:
        if "debug_enabled" in cols:
            batch_op.drop_column("debug_enabled")
