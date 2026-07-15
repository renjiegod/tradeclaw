"""Rename provider -> model_id in model_invocations

Revision ID: 20260429_03
Revises: 20260429_02
Create Date: 2026-04-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260429_03"
down_revision = "20260429_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_invocations") as batch_op:
        batch_op.alter_column("provider", new_column_name="model_id", type_=sa.String(length=255), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("model_invocations") as batch_op:
        batch_op.alter_column("model_id", new_column_name="provider", type_=sa.String(length=32), nullable=False)
