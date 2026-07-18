"""Add entity lifecycle, lineage, conflicts, evidence, and canvas layouts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260718_05"
down_revision = "20260718_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("kg_nodes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(sa.Column("retired_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("redirect_to_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_kg_nodes_status",
            "status IN ('active', 'retired', 'merged')",
        )
        batch_op.create_index("ix_kg_nodes_status", ["status"], unique=False)

    op.create_table(
        "kg_entity_lineage",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("survivor_id", sa.String(length=64), nullable=True),
        sa.Column("source_ids_json", sa.JSON(), nullable=False),
        sa.Column("result_ids_json", sa.JSON(), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('merge', 'split')",
            name="ck_kg_entity_lineage_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kg_entity_lineage_survivor",
        "kg_entity_lineage",
        ["survivor_id"],
        unique=False,
    )
    op.create_index(
        "ix_kg_entity_lineage_revision",
        "kg_entity_lineage",
        ["revision"],
        unique=False,
    )

    op.create_table(
        "kg_conflicts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conflict_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("subject_key", sa.String(length=255), nullable=False),
        sa.Column("left_json", sa.JSON(), nullable=False),
        sa.Column("right_json", sa.JSON(), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column("change_set_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "conflict_type IN ("
            "'dedupe', 'state_key', 'manual_vs_auto', 'identity', 'draft_stale'"
            ")",
            name="ck_kg_conflicts_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_kg_conflicts_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kg_conflicts_status_detected",
        "kg_conflicts",
        ["status", "detected_at"],
        unique=False,
    )
    op.create_index(
        "ix_kg_conflicts_subject",
        "kg_conflicts",
        ["subject_key"],
        unique=False,
    )

    op.create_table(
        "kg_evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("detached_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "target_kind IN ('node', 'edge')",
            name="ck_kg_evidence_target_kind",
        ),
        sa.CheckConstraint(
            "kind IN ('kb_ref', 'url', 'quote', 'file')",
            name="ck_kg_evidence_kind",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'detached')",
            name="ck_kg_evidence_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kg_evidence_target",
        "kg_evidence",
        ["target_kind", "target_id"],
        unique=False,
    )
    op.create_index(
        "ix_kg_evidence_status",
        "kg_evidence",
        ["status"],
        unique=False,
    )

    op.create_table(
        "kg_canvas_layouts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("positions_json", sa.JSON(), nullable=False),
        sa.Column("locked_ids_json", sa.JSON(), nullable=False),
        sa.Column("highlight_ids_json", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("change_set_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "version",
            name="uq_kg_canvas_layouts_scope_version",
        ),
    )
    op.create_index(
        "ix_kg_canvas_layouts_scope",
        "kg_canvas_layouts",
        ["scope_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kg_canvas_layouts_scope", table_name="kg_canvas_layouts")
    op.drop_table("kg_canvas_layouts")
    op.drop_index("ix_kg_evidence_status", table_name="kg_evidence")
    op.drop_index("ix_kg_evidence_target", table_name="kg_evidence")
    op.drop_table("kg_evidence")
    op.drop_index("ix_kg_conflicts_subject", table_name="kg_conflicts")
    op.drop_index("ix_kg_conflicts_status_detected", table_name="kg_conflicts")
    op.drop_table("kg_conflicts")
    op.drop_index("ix_kg_entity_lineage_revision", table_name="kg_entity_lineage")
    op.drop_index("ix_kg_entity_lineage_survivor", table_name="kg_entity_lineage")
    op.drop_table("kg_entity_lineage")
    with op.batch_alter_table("kg_nodes") as batch_op:
        batch_op.drop_index("ix_kg_nodes_status")
        batch_op.drop_constraint("ck_kg_nodes_status", type_="check")
        batch_op.drop_column("redirect_to_id")
        batch_op.drop_column("retired_at")
        batch_op.drop_column("status")
