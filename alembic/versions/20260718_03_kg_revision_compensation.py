"""Track compensating knowledge-graph revisions for undo and redo."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_03"
down_revision = "20260718_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_revisions",
        sa.Column("reverts_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "kg_revisions",
        sa.Column("replays_revision", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("kg_revisions", "replays_revision")
    op.drop_column("kg_revisions", "reverts_revision")
