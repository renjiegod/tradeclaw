"""Drop deprecated strategy graph tables.

Revision ID: 20260505_01
Revises: 20260502_07
Create Date: 2026-05-05 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260505_01"
down_revision = "20260502_07"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("strategy_graph_edges"):
        op.drop_index("ix_strategy_graph_edges_from_to", table_name="strategy_graph_edges")
        op.drop_table("strategy_graph_edges")
    if _has_table("strategy_graph_nodes"):
        op.drop_index("ix_strategy_graph_nodes_sort_order", table_name="strategy_graph_nodes")
        op.drop_index("ix_strategy_graph_nodes_instance_id", table_name="strategy_graph_nodes")
        op.drop_table("strategy_graph_nodes")
    if _has_table("strategy_graphs"):
        op.drop_index("ix_strategy_graphs_status", table_name="strategy_graphs")
        op.drop_index("ix_strategy_graphs_name", table_name="strategy_graphs")
        op.drop_table("strategy_graphs")


def downgrade() -> None:
    # Graph runtime support has been removed from the codebase.
    pass
