"""Drop legacy generated_strategies table.

Revision ID: 20260502_04
Revises: 20260502_03
Create Date: 2026-05-02 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260502_04"
down_revision = "20260502_03"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "generated_strategies"):
        return
    op.drop_table("generated_strategies")


def downgrade() -> None:
    pass
