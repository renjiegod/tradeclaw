"""Durable channel peer → active-session routing.

Introduces ``channel_peer_sessions`` backing the ``ChannelManager``'s move of
the peer→active-session rebinding (triggered by a channel ``/new``) from a
process-local ``dict`` (``ChannelManager._active_peer_sessions``) to a durable
store. Without this, a ``/new`` issued from a channel (e.g. Feishu) was lost on
server restart, silently snapping the peer back to its old deterministic
``channel:{channel_id}:{sender_id}`` session. The in-memory map remains as a hot
cache seeded from this table.

``peer_session_id`` is ``String(128)`` because the deterministic peer id is
``channel:{channel_id}:{sender_id}`` and can reach ~64 chars for Feishu alone.
No FK on ``active_session_id`` — matches the existing
``assistant_messages`` / ``assistant_events`` convention (session ownership is
checked in code, not via FK), and a stale pointer harmlessly falls back through
``get_or_create_session``.

Per ``project_no_backcompat_phase`` there is no prior on-disk routing state to
migrate — the table starts empty.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260527_01"
down_revision = "20260526_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_peer_sessions",
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("peer_session_id", sa.String(length=128), nullable=False),
        sa.Column("active_session_id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "peer_session_id"),
    )


def downgrade() -> None:
    op.drop_table("channel_peer_sessions")
