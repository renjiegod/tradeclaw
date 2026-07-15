"""Seed the code-fixed signal-card composer agent row.

A second code-fixed builtin agent (companion to ``agent_default``): the
「信号卡片撰写器」 / ``agent_signal_composer``. Its identity (name / prompt
template / ``is_builtin``) is re-pinned on every boot by
``SqlAlchemyAgentRepository.ensure_signal_composer_agent``; this migration only
makes sure the row exists for deployments that already have the ``agents``
table, so trigger prose delivery can resolve it by id before the first boot
re-pins it.

The agent is compose-only: it deliberately carries empty ``tool_names`` /
``skill_names`` (the service load points only expand the *main* agent's
capabilities; this one stays empty at runtime too). ``is_default`` stays False
— it never serves general routing, only explicit prose-compose turns.

Additive and idempotent: inserts the row if the table exists and the id is
missing; never touches an existing row (boot re-pins it).

Revision ID: 20260615_01
Revises: 20260614_04
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260615_01"
down_revision = "20260614_04"
branch_labels = None
depends_on = None

_TABLE = "agents"

# Keep in sync with doyoutrade/assistant/signal_composer_agent.py.
_COMPOSER_ID = "agent_signal_composer"
_COMPOSER_NAME = "信号卡片撰写器"
_COMPOSER_PROMPT_TEMPLATE_ID = "signal-card-composer"


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_table(_TABLE):
        return

    meta = sa.MetaData()
    agents = sa.Table(
        _TABLE,
        meta,
        sa.Column("id", sa.String(64)),
        sa.Column("name", sa.String(255)),
        sa.Column("status", sa.String(32)),
        sa.Column("system_prompt", sa.Text),
        sa.Column("system_prompt_template_id", sa.String(128)),
        sa.Column("model_route_name", sa.String(128)),
        sa.Column("tool_names", sa.JSON),
        sa.Column("tool_configs_json", sa.JSON),
        sa.Column("skill_names", sa.JSON),
        sa.Column("max_turns", sa.Integer),
        sa.Column("is_default", sa.Boolean),
        sa.Column("is_builtin", sa.Boolean),
        sa.Column("context_compaction_json", sa.JSON),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    bind = op.get_bind()
    # Inherit the main agent's model route so the composer is callable on first
    # boot (an empty route resolves to the keyless baseline and would 500 on the
    # compose turn). Boot's ensure_signal_composer_agent re-applies this inherit
    # rule on every start, but seeding it here avoids a blank-route window.
    main_route = bind.execute(
        sa.select(agents.c.model_route_name).where(agents.c.id == "agent_default")
    ).scalar_one_or_none()
    existing = bind.execute(
        sa.select(agents.c.id).where(agents.c.id == _COMPOSER_ID)
    ).scalar_one_or_none()
    if existing is not None:
        # Boot owns the row's identity; never mutate it from a migration.
        return
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    bind.execute(
        agents.insert().values(
            id=_COMPOSER_ID,
            name=_COMPOSER_NAME,
            status="active",
            system_prompt="",
            system_prompt_template_id=_COMPOSER_PROMPT_TEMPLATE_ID,
            model_route_name=str(main_route or ""),
            tool_names=[],
            tool_configs_json=[],
            skill_names=[],
            max_turns=6,
            is_default=False,
            is_builtin=True,
            context_compaction_json=None,
            created_at=now,
            updated_at=now,
        )
    )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    meta = sa.MetaData()
    agents = sa.Table(_TABLE, meta, sa.Column("id", sa.String(64)))
    op.execute(agents.delete().where(agents.c.id == _COMPOSER_ID))
