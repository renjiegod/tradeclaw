"""Strengthen knowledge-graph projection ownership and active-edge integrity."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_01"
down_revision = "20260717_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_edges",
        sa.Column("source_key", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE kg_edges
            SET source_key = CASE
                WHEN source_ref LIKE 'db:decision_signals/%'
                    THEN 'db:decision_signals'
                ELSE source_ref
            END
            WHERE source_key IS NULL
            """
        )
    )

    # Historical application races could have left more than one active row
    # for a dedupe key. Keep the newest row and expire every older duplicate
    # before installing the database-level invariant.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY dedupe_key
                        ORDER BY created_at DESC, id DESC
                    ) AS row_number
                FROM kg_edges
                WHERE expired_at IS NULL
            )
            UPDATE kg_edges
            SET expired_at = CURRENT_TIMESTAMP
            WHERE id IN (
                SELECT id FROM ranked WHERE row_number > 1
            )
            """
        )
    )
    op.create_index(
        "ix_kg_edges_source_active",
        "kg_edges",
        ["source_key", "expired_at"],
        unique=False,
    )
    op.create_index(
        "uq_kg_edges_active_dedupe",
        "kg_edges",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text("expired_at IS NULL"),
        postgresql_where=sa.text("expired_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_kg_edges_active_dedupe", table_name="kg_edges")
    op.drop_index("ix_kg_edges_source_active", table_name="kg_edges")
    op.drop_column("kg_edges", "source_key")
