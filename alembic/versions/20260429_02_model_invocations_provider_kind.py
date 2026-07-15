"""Add provider_kind to model_invocations

Revision ID: 20260429_02
Revises: 20260429_01
Create Date: 2026-04-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260429_02"
down_revision = "20260429_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite does not support ALTER COLUMN ... SET NOT NULL, so we use
    # batch operations to recreate the table with the correct schema.
    with op.batch_alter_table("model_invocations") as batch_op:
        batch_op.add_column(
            sa.Column("provider_kind", sa.String(length=32), nullable=True)
        )
    # Backfill: existing rows stored provider_kind in the `provider` column
    # (the old buggy code assigned provider_kind to the provider field).
    op.execute(
        "UPDATE model_invocations SET provider_kind = provider WHERE provider_kind IS NULL"
    )
    with op.batch_alter_table("model_invocations") as batch_op:
        batch_op.alter_column("provider_kind", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("model_invocations") as batch_op:
        batch_op.drop_column("provider_kind")
