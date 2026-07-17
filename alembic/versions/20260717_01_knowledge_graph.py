"""知识图谱三表（个人交易知识库的实体/关系层）。

- ``kg_nodes`` —— 实体节点（symbol / role / cycle / signal ...），自然键
  ``(node_type, name)`` 唯一，``id`` 为 ``kgn-`` 代理键。
- ``kg_edges`` —— bi-temporal 事实边：现实轴 ``valid_at``/``invalid_at`` +
  系统轴 ``created_at``/``expired_at``（旧事实只 expire 不删除）；
  ``dedupe_key`` 支撑幂等投影，``state_key`` 支撑单值状态组失效
  （如个股角色变更时旧角色边自动过期）；``provenance`` 区分确定性投影
  与 LLM 抽取。真实 FK → kg_nodes ON DELETE CASCADE。
- ``kg_source_state`` —— 投影来源的 content_hash 增量水位。

backing models 见 :mod:`doyoutrade.persistence.models` 的
``KnowledgeGraphNodeRecord`` / ``KnowledgeGraphEdgeRecord`` /
``KnowledgeGraphSourceStateRecord``。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260717_01"
down_revision = "20260713_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_nodes",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_type", "name", name="uq_kg_nodes_type_name"),
    )
    op.create_index("ix_kg_nodes_name", "kg_nodes", ["name"], unique=False)

    op.create_table(
        "kg_edges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("src_id", sa.String(length=64), nullable=False),
        sa.Column("dst_id", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=64), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("state_key", sa.String(length=255), nullable=True),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("valid_at", sa.DateTime(), nullable=True),
        sa.Column("invalid_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expired_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["src_id"], ["kg_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst_id"], ["kg_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "provenance IN ('deterministic', 'llm')",
            name="ck_kg_edges_provenance",
        ),
    )
    op.create_index("ix_kg_edges_src_active", "kg_edges", ["src_id", "expired_at"], unique=False)
    op.create_index("ix_kg_edges_dst_active", "kg_edges", ["dst_id", "expired_at"], unique=False)
    op.create_index("ix_kg_edges_dedupe", "kg_edges", ["dedupe_key", "expired_at"], unique=False)
    op.create_index("ix_kg_edges_state_key", "kg_edges", ["state_key"], unique=False)

    op.create_table(
        "kg_source_state",
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("source"),
    )


def downgrade() -> None:
    op.drop_table("kg_source_state")
    op.drop_index("ix_kg_edges_state_key", table_name="kg_edges")
    op.drop_index("ix_kg_edges_dedupe", table_name="kg_edges")
    op.drop_index("ix_kg_edges_dst_active", table_name="kg_edges")
    op.drop_index("ix_kg_edges_src_active", table_name="kg_edges")
    op.drop_table("kg_edges")
    op.drop_index("ix_kg_nodes_name", table_name="kg_nodes")
    op.drop_table("kg_nodes")
