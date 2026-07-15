"""Add ``is_builtin`` to ``agents`` and mark the code-fixed main agent.

The seeded ``agent_default`` row is being promoted to a code-level *fixed main
agent*: undeletable / unrenamable, with skills / tools / system prompt
controlled in code (``doyoutrade/assistant/main_agent.py`` + ``main_agent.j2``),
while only ``model_route_name`` / ``context_compaction`` / ``max_turns`` stay
user-editable. ``is_builtin`` is the explicit marker that distinguishes it from
custom agents — kept separate from ``is_default`` (the routing fallback) so the
two concepts don't get conflated.

Additive, idempotent: new ``is_builtin BOOLEAN NOT NULL DEFAULT false`` column,
then backfill ``true`` for the existing builtin row (``agent_default``, or any
historical ``is_default`` row).

Revision ID: 20260614_02
Revises: 20260614_01
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260614_02"
down_revision = "20260614_01"
branch_labels = None
depends_on = None

_TABLE = "agents"
_COLUMN = "is_builtin"

# Keep in sync with doyoutrade/assistant/main_agent.py::MAIN_AGENT_ID.
_MAIN_AGENT_ID = "agent_default"


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(col["name"] == column for col in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    # Dialect-safe backfill via a lightweight table construct (SQLAlchemy renders
    # boolean literals per dialect — avoids ``is_default = 1`` failing on Postgres).
    meta = sa.MetaData()
    agents = sa.Table(
        _TABLE,
        meta,
        sa.Column("id", sa.String(64)),
        sa.Column("is_default", sa.Boolean()),
        sa.Column("is_builtin", sa.Boolean()),
    )
    op.execute(
        agents.update()
        .where(
            sa.or_(
                agents.c.id == _MAIN_AGENT_ID,
                agents.c.is_default == sa.true(),
            )
        )
        .values(is_builtin=True)
    )


def downgrade() -> None:
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
