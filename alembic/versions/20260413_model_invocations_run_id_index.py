"""Index model_invocations.run_id for list-by-run queries.

Revision ID: 20260413_01
Revises: 20260412_01
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op

revision = "20260413_01"
down_revision = "20260412_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_model_invocations_run_id",
        "model_invocations",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_model_invocations_run_id", table_name="model_invocations")
