"""Model providers, model routes, instance/backtest/invocation route columns.

Revision ID: 20260419_02
Revises: 20260419_01
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260419_02"
down_revision = "20260419_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.add_column(
        "instances",
        sa.Column("model_route_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "backtest_jobs",
        sa.Column("model_route_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "model_invocations",
        sa.Column("model_route_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "model_invocations",
        sa.Column("provider_key", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_invocations", "provider_key")
    op.drop_column("model_invocations", "model_route_name")
    op.drop_column("backtest_jobs", "model_route_name")
    op.drop_column("instances", "model_route_name")
    op.drop_table("model_routes")
    op.drop_table("model_providers")
