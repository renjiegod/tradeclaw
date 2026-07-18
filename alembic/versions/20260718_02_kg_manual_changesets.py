"""Add durable graph changesets, revisions, and one-time approvals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_02"
down_revision = "20260718_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("kg_edges") as batch:
        batch.drop_constraint("ck_kg_edges_provenance", type_="check")
        batch.create_check_constraint(
            "ck_kg_edges_provenance",
            "provenance IN ('deterministic', 'llm', 'manual')",
        )

    op.create_table(
        "kg_graph_state",
        sa.Column("state_key", sa.String(length=32), nullable=False),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state_key"),
    )
    op.execute(
        sa.text(
            "INSERT INTO kg_graph_state "
            "(state_key, head_revision, updated_at) "
            "VALUES ('default', 0, CURRENT_TIMESTAMP)"
        )
    )
    op.create_table(
        "kg_change_sets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'stale', 'cancelled')",
            name="ck_kg_change_sets_status",
        ),
        sa.CheckConstraint(
            "actor_type IN ('local_user', 'agent', 'system')",
            name="ck_kg_change_sets_actor_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision"),
    )
    op.create_index(
        "ix_kg_change_sets_status_created",
        "kg_change_sets",
        ["status", "created_at"],
        unique=False,
    )
    op.create_table(
        "kg_change_operations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("op_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["kg_change_sets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "change_set_id",
            "position",
            name="uq_kg_change_operations_position",
        ),
    )
    op.create_index(
        "ix_kg_change_operations_change_set",
        "kg_change_operations",
        ["change_set_id", "position"],
        unique=False,
    )
    op.create_table(
        "kg_revisions",
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["change_set_id"], ["kg_change_sets.id"]),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint("change_set_id"),
    )
    op.create_table(
        "kg_approval_decisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("proposal_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("resolver_id", sa.String(length=128), nullable=False),
        sa.Column("decision_source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_kg_approval_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["kg_change_sets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "change_set_id",
            name="uq_kg_approval_decisions_change_set",
        ),
    )


def downgrade() -> None:
    op.drop_table("kg_approval_decisions")
    op.drop_table("kg_revisions")
    op.drop_index(
        "ix_kg_change_operations_change_set",
        table_name="kg_change_operations",
    )
    op.drop_table("kg_change_operations")
    op.drop_index(
        "ix_kg_change_sets_status_created",
        table_name="kg_change_sets",
    )
    op.drop_table("kg_change_sets")
    op.drop_table("kg_graph_state")

    op.execute(sa.text("DELETE FROM kg_edges WHERE provenance = 'manual'"))
    with op.batch_alter_table("kg_edges") as batch:
        batch.drop_constraint("ck_kg_edges_provenance", type_="check")
        batch.create_check_constraint(
            "ck_kg_edges_provenance",
            "provenance IN ('deterministic', 'llm')",
        )
