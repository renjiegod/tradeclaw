"""Add monitor_rules + monitor_alerts (standalone realtime 盯盘规则).

Introduces first-class, stock-scoped monitoring rules evaluated tick-by-tick by
the MonitorDaemon against the realtime quote stream — independent of any Task
(unlike task_triggers, which only fire while their parent task is running).

- ``monitor_rules``: a scope (watchlist_tag | symbols) + an AND/OR condition tree
  (preset/predicate leaves) + a delivery binding (same shape as
  task_triggers.delivery_json) + a per-rule cooldown.
- ``monitor_alerts``: append-only fire history; also the durable cooldown/dedup
  source. ``run_id`` threads the per-fire run into debug_sessions
  (session_type='monitor') / spans so a fire is reachable by run_id like a cycle
  run: run_id <-> debug_sessions <-> debug_session_spans.

Purely additive. JSON columns carry no server_default (SQLite/Postgres
inconsistency); the ORM fills {} via default=dict.

Revision ID: 20260620_01
Revises: 20260619_01
Create Date: 2026-06-20 00:00:01.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260620_01"
down_revision = "20260619_01"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(ix["name"] == index for ix in inspect(bind).get_indexes(table))


def upgrade() -> None:
    if not _has_table("monitor_rules"):
        op.create_table(
            "monitor_rules",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("scope_kind", sa.String(length=32), nullable=False),
            sa.Column("scope_json", sa.JSON(), nullable=False),
            sa.Column("condition_json", sa.JSON(), nullable=False),
            sa.Column("delivery_json", sa.JSON(), nullable=True),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "scope_kind IN ('watchlist_tag', 'symbols')",
                name="ck_monitor_rules_scope_kind",
            ),
            sa.CheckConstraint(
                "status IN ('active', 'paused', 'error')",
                name="ck_monitor_rules_status",
            ),
        )
        op.create_index("ix_monitor_rules_active", "monitor_rules", ["enabled", "status"])
        op.create_index("ix_monitor_rules_created_at", "monitor_rules", ["created_at"])

    if not _has_table("monitor_alerts"):
        op.create_table(
            "monitor_alerts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "monitor_rule_id",
                sa.String(length=64),
                sa.ForeignKey("monitor_rules.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("condition_name", sa.String(length=64), nullable=False),
            sa.Column("transition_key", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("triggered_at", sa.DateTime(), nullable=False),
            sa.Column("last_price", sa.Float(), nullable=True),
            sa.Column("limit_price", sa.Float(), nullable=True),
            sa.Column("diagnostics_json", sa.JSON(), nullable=False),
            sa.Column("run_id", sa.String(length=80), nullable=True),
            sa.Column("delivery_status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_monitor_alerts_rule_symbol", "monitor_alerts", ["monitor_rule_id", "symbol"]
        )
        op.create_index(
            "ix_monitor_alerts_dedup",
            "monitor_alerts",
            ["monitor_rule_id", "symbol", "condition_name", "triggered_at"],
        )
        op.create_index("ix_monitor_alerts_triggered_at", "monitor_alerts", ["triggered_at"])
        op.create_index("ix_monitor_alerts_run_id", "monitor_alerts", ["run_id"])


def downgrade() -> None:
    if _has_table("monitor_alerts"):
        for ix in (
            "ix_monitor_alerts_run_id",
            "ix_monitor_alerts_triggered_at",
            "ix_monitor_alerts_dedup",
            "ix_monitor_alerts_rule_symbol",
        ):
            if _has_index("monitor_alerts", ix):
                op.drop_index(ix, table_name="monitor_alerts")
        op.drop_table("monitor_alerts")
    if _has_table("monitor_rules"):
        for ix in ("ix_monitor_rules_created_at", "ix_monitor_rules_active"):
            if _has_index("monitor_rules", ix):
                op.drop_index(ix, table_name="monitor_rules")
        op.drop_table("monitor_rules")
