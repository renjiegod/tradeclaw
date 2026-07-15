"""Strategy code moves to on-disk versioned directories.

Drops ``source_code`` (Text) and the ``class_name`` column from
``strategy_definitions`` and adds ``current_version`` (TEXT, nullable
while there is no draft yet promoted).

Existing rows are deleted: pre-prod project policy, no data migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260525_01"
down_revision = "20260524_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Delete child rows first to satisfy the FK constraint on strategy_instances.
    op.execute("DELETE FROM strategy_instances")
    op.execute("DELETE FROM strategy_definitions")
    with op.batch_alter_table("strategy_definitions") as batch:
        batch.drop_column("source_code")
        batch.drop_column("class_name")
        batch.add_column(
            sa.Column("current_version", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    # Downgrade re-adds NOT NULL columns with empty server defaults. No
    # row data is reconstructed — the upstream upgrade deletes everything.
    with op.batch_alter_table("strategy_definitions") as batch:
        batch.drop_column("current_version")
        batch.add_column(sa.Column("source_code", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("class_name", sa.Text(), nullable=False, server_default=""))
