"""Persisted QMT accounts (multi-account, replaces config.data.qmt).

Each row is one QMT proxy connection + trading identity + mode (live|mock).
Tasks select one via ``tasks.account_id``; exactly one row should carry
``is_default=True`` (enforced transactionally by the repository's set_default,
not a DB partial-unique index). No seeding / data-copy shim (No-backcompat).

Revision ID: 20260601_01
Revises: 20260530_01
Create Date: 2026-06-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260601_01"
down_revision = "20260530_01"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("accounts"):
        return
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="live"),
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column(
            "timeout_seconds", sa.Float(), nullable=False, server_default="5.0",
        ),
        sa.Column("qmt_account_id", sa.String(length=128), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("mock_cash", sa.Float(), nullable=False, server_default="100000.0"),
        sa.Column(
            "mock_equity", sa.Float(), nullable=False, server_default="100000.0",
        ),
        # JSON server_default behaves inconsistently across SQLite/Postgres,
        # so the column is nullable and the ORM fills [] via default=list.
        sa.Column("mock_positions", sa.JSON(), nullable=True),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("mode IN ('live','mock')", name="ck_accounts_mode"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_is_default", "accounts", ["is_default"], unique=False)
    op.create_index("ix_accounts_enabled", "accounts", ["enabled"], unique=False)


def downgrade() -> None:
    if not _has_table("accounts"):
        return
    op.drop_index("ix_accounts_enabled", table_name="accounts")
    op.drop_index("ix_accounts_is_default", table_name="accounts")
    op.drop_table("accounts")
