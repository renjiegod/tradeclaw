"""Add versioned custom knowledge-graph Schema items."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_04"
down_revision = "20260718_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_schema_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('entity_type', 'relation_type', 'property')",
            name="ck_kg_schema_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated')",
            name="ck_kg_schema_items_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind",
            "key",
            name="uq_kg_schema_items_kind_key",
        ),
    )
    op.create_index(
        "ix_kg_schema_items_kind_status",
        "kg_schema_items",
        ["kind", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kg_schema_items_kind_status",
        table_name="kg_schema_items",
    )
    op.drop_table("kg_schema_items")
