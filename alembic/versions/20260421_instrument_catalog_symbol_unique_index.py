"""Named unique index on instrument_catalog.symbol (canonical code).

``symbol`` is already the primary key; this adds an explicit ``uq_*`` unique index
for clarity and tooling. Safe to apply on top of existing PK.

Revision ID: 20260421_01
Revises: 20260419_02
Create Date: 2026-04-21
"""

from __future__ import annotations

from alembic import op

revision = "20260421_01"
down_revision = "20260419_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_instrument_catalog_symbol",
        "instrument_catalog",
        ["symbol"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_instrument_catalog_symbol", table_name="instrument_catalog")
