"""Merge model_providers into model_routes as a single self-contained model config.

Formerly ``model_routes`` referenced ``model_providers`` (connection + credentials)
via a FK. They are merged so one row fully describes a model: connection/credential
columns move onto ``model_routes`` and ``model_providers`` is dropped.

No data migration: existing dev/test rows are dropped and the table is rebuilt with
the new shape (per the merge decision). If production data ever needs preserving,
write a data-copy migration instead.

Revision ID: 20260704_01
Revises: 20260620_02
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260704_01"
down_revision = "20260620_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the FK-linked route table and the provider table, then rebuild
    # model_routes as a single self-contained entity.
    op.drop_table("model_routes")
    op.drop_table("model_providers")
    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("route_name", sa.String(length=128), nullable=False),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("target_model", sa.String(length=255), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("route_name", name="uq_model_routes_route_name"),
    )


def downgrade() -> None:
    op.drop_table("model_routes")
    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_key", sa.String(length=128), nullable=False),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("target_model", sa.String(length=255), nullable=True),
        sa.Column("extra_config", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_key", name="uq_model_providers_provider_key"),
    )
    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("route_name", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("target_model", sa.String(length=255), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["model_providers.id"],
            name="fk_model_routes_provider_id_model_providers",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("route_name", name="uq_model_routes_route_name"),
    )
