"""Add strategy registry persistence tables.

Revision ID: 20260502_03
Revises: 20260502_02
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260502_03"
down_revision = "20260502_02"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "strategy_definitions"):
        op.create_table(
            "strategy_definitions",
            sa.Column("definition_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("class_name", sa.String(length=255), nullable=False),
            sa.Column("source_code", sa.Text(), nullable=False),
            sa.Column("api_version", sa.String(length=32), nullable=False),
            sa.Column("input_contract_json", sa.JSON(), nullable=True),
            sa.Column("parameter_schema_json", sa.JSON(), nullable=True),
            sa.Column("default_parameters_json", sa.JSON(), nullable=True),
            sa.Column("capabilities_json", sa.JSON(), nullable=True),
            sa.Column("provenance_json", sa.JSON(), nullable=True),
            sa.Column("code_hash", sa.String(length=128), nullable=False),
            sa.Column("generation_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("generation_model", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("generation_metadata_json", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("definition_id"),
        )
        op.create_index(
            "ix_strategy_definitions_name",
            "strategy_definitions",
            ["name"],
            unique=False,
        )
        op.create_index(
            "ix_strategy_definitions_status",
            "strategy_definitions",
            ["status"],
            unique=False,
        )

    if not _has_table(bind, "strategy_instances"):
        op.create_table(
            "strategy_instances",
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.Column("definition_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("parameters_json", sa.JSON(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("tags_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(
                ["definition_id"],
                ["strategy_definitions.definition_id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("instance_id"),
        )
        op.create_index(
            "ix_strategy_instances_definition_id",
            "strategy_instances",
            ["definition_id"],
            unique=False,
        )
        op.create_index(
            "ix_strategy_instances_name",
            "strategy_instances",
            ["name"],
            unique=False,
        )

    if not _has_table(bind, "strategy_graphs"):
        op.create_table(
            "strategy_graphs",
            sa.Column("graph_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("execution_mode", sa.String(length=32), nullable=False),
            sa.Column("merge_policy_json", sa.JSON(), nullable=True),
            sa.Column("graph_parameters_json", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("graph_id"),
        )
        op.create_index(
            "ix_strategy_graphs_name",
            "strategy_graphs",
            ["name"],
            unique=False,
        )
        op.create_index(
            "ix_strategy_graphs_status",
            "strategy_graphs",
            ["status"],
            unique=False,
        )

    if not _has_table(bind, "strategy_graph_nodes"):
        op.create_table(
            "strategy_graph_nodes",
            sa.Column("graph_id", sa.String(length=64), nullable=False),
            sa.Column("node_id", sa.String(length=64), nullable=False),
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.Column("node_role", sa.String(length=64), nullable=False),
            sa.Column("node_config_json", sa.JSON(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.ForeignKeyConstraint(["graph_id"], ["strategy_graphs.graph_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["instance_id"],
                ["strategy_instances.instance_id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("graph_id", "node_id"),
        )
        op.create_index(
            "ix_strategy_graph_nodes_instance_id",
            "strategy_graph_nodes",
            ["instance_id"],
            unique=False,
        )
        op.create_index(
            "ix_strategy_graph_nodes_sort_order",
            "strategy_graph_nodes",
            ["graph_id", "sort_order"],
            unique=False,
        )

    if not _has_table(bind, "strategy_graph_edges"):
        op.create_table(
            "strategy_graph_edges",
            sa.Column("graph_id", sa.String(length=64), nullable=False),
            sa.Column("edge_id", sa.String(length=64), nullable=False),
            sa.Column("from_node_id", sa.String(length=64), nullable=False),
            sa.Column("to_node_id", sa.String(length=64), nullable=False),
            sa.Column("edge_type", sa.String(length=64), nullable=False),
            sa.Column("edge_config_json", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["graph_id"], ["strategy_graphs.graph_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("graph_id", "edge_id"),
        )
        op.create_index(
            "ix_strategy_graph_edges_from_to",
            "strategy_graph_edges",
            ["graph_id", "from_node_id", "to_node_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "strategy_graph_edges"):
        op.drop_index("ix_strategy_graph_edges_from_to", table_name="strategy_graph_edges")
        op.drop_table("strategy_graph_edges")

    if _has_table(bind, "strategy_graph_nodes"):
        op.drop_index("ix_strategy_graph_nodes_sort_order", table_name="strategy_graph_nodes")
        op.drop_index("ix_strategy_graph_nodes_instance_id", table_name="strategy_graph_nodes")
        op.drop_table("strategy_graph_nodes")

    if _has_table(bind, "strategy_graphs"):
        op.drop_index("ix_strategy_graphs_status", table_name="strategy_graphs")
        op.drop_index("ix_strategy_graphs_name", table_name="strategy_graphs")
        op.drop_table("strategy_graphs")

    if _has_table(bind, "strategy_instances"):
        op.drop_index("ix_strategy_instances_name", table_name="strategy_instances")
        op.drop_index("ix_strategy_instances_definition_id", table_name="strategy_instances")
        op.drop_table("strategy_instances")

    if _has_table(bind, "strategy_definitions"):
        op.drop_index("ix_strategy_definitions_status", table_name="strategy_definitions")
        op.drop_index("ix_strategy_definitions_name", table_name="strategy_definitions")
        op.drop_table("strategy_definitions")
