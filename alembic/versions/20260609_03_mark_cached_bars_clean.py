"""Mark cached_bars table as clean after adjusting default adjust from qfq to none.

Revision ID: 20260609_03
Revises: 20260609_02
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260609_03"
down_revision = "20260609_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 添加标记列到 cached_bars 和 cached_bar_ranges 表
    # 注意：这里不修改数据，只添加状态列
    # cached_bars
    try:
        op.add_column(
            "cached_bars",
            sa.Column("data_cleaned", sa.Boolean(), nullable=False, server_default="false"),
        )
    except Exception:
        # 列可能已存在（回滚后重新运行 upgrade）
        pass

    # cached_bar_ranges
    try:
        op.add_column(
            "cached_bar_ranges",
            sa.Column("data_cleaned", sa.Boolean(), nullable=False, server_default="false"),
        )
    except Exception:
        # 列可能已存在（回滚后重新运行 upgrade）
        pass


def downgrade() -> None:
    # 移除标记列
    try:
        op.drop_column("cached_bars", "data_cleaned")
    except Exception:
        pass
    try:
        op.drop_column("cached_bar_ranges", "data_cleaned")
    except Exception:
        pass