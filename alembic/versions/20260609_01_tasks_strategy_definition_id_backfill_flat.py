"""Backfill tasks.strategy_definition_id from flat settings key.

Revision 20260608_01 only matched nested ``settings.strategy.definition_id``.
Most persisted tasks store ``settings.strategy_definition_id`` instead; this
migration re-backfills rows whose denormalized column is still empty.

Revision ID: 20260609_01
Revises: 20260608_01
Create Date: 2026-06-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260609_01"
down_revision = "20260608_01"
branch_labels = None
depends_on = None


def _backfill_missing_strategy_definition_id() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        bind.execute(
            sa.text(
                """
                UPDATE tasks
                SET strategy_definition_id = COALESCE(
                    NULLIF(trim(json_extract(settings, '$.strategy.definition_id')), ''),
                    NULLIF(trim(json_extract(settings, '$.strategy_definition_id')), '')
                )
                WHERE settings IS NOT NULL
                  AND (strategy_definition_id IS NULL OR trim(strategy_definition_id) = '')
                  AND COALESCE(
                    NULLIF(trim(json_extract(settings, '$.strategy.definition_id')), ''),
                    NULLIF(trim(json_extract(settings, '$.strategy_definition_id')), '')
                  ) IS NOT NULL
                """
            )
        )
        return
    if dialect == "postgresql":
        bind.execute(
            sa.text(
                """
                UPDATE tasks
                SET strategy_definition_id = COALESCE(
                    NULLIF(trim(settings->'strategy'->>'definition_id'), ''),
                    NULLIF(trim(settings->>'strategy_definition_id'), '')
                )
                WHERE settings IS NOT NULL
                  AND (strategy_definition_id IS NULL OR trim(strategy_definition_id) = '')
                  AND COALESCE(
                    NULLIF(trim(settings->'strategy'->>'definition_id'), ''),
                    NULLIF(trim(settings->>'strategy_definition_id'), '')
                  ) IS NOT NULL
                """
            )
        )
        return
    raise RuntimeError(
        f"unsupported database dialect for 20260609_01 backfill: {dialect}"
    )


def upgrade() -> None:
    _backfill_missing_strategy_definition_id()


def downgrade() -> None:
    # Data backfill is not reversed; column from 20260608_01 remains.
    pass
