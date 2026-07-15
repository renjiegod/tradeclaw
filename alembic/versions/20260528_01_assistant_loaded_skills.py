"""Persistent storage for assistant load_skill invocations.

Introduces ``assistant_loaded_skills`` backing the move of "which SKILL.md
files this session has loaded, plus their body" from a process-local
dict on the assistant service to a durable store. Without this, context
compaction (which folds old tool_results into a summary boundary) drops
the skill body, and a follow-up turn that needs the skill has no choice
but to either re-invoke ``load_skill`` (wasteful) or fly blind. The
assistant service rebuilds a ``<system-reminder>`` from these rows after
a compaction so the agent keeps the skill loaded across the boundary.

Composite PK on ``(session_id, skill_name)`` gives natural upsert
semantics when the same skill is re-loaded; FK CASCADE on
``session_id`` -> ``assistant_sessions.session_id`` lets session
deletion clean up the rows automatically.

Per ``project_no_backcompat_phase`` there is no prior on-disk state to
migrate — the table starts empty.

Revision ID: 20260528_01
Revises: 20260527_02
Create Date: 2026-05-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_01"
down_revision = "20260527_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_loaded_skills",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("skill_path", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "skill_name"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["assistant_sessions.session_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_assistant_loaded_skills_session_id",
        "assistant_loaded_skills",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_loaded_skills_session_id",
        table_name="assistant_loaded_skills",
    )
    op.drop_table("assistant_loaded_skills")
