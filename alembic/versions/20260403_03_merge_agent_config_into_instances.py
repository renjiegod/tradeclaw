"""merge agent_configs into instances

Revision ID: 20260403_03
Revises: 20260403_02
Create Date: 2026-04-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260403_03"
down_revision = "20260403_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    watch_default = sa.text("'[]'::json") if is_pg else sa.text("'[]'")
    str_empty = sa.text("''")

    op.add_column(
        "instances",
        sa.Column("watch_symbols", sa.JSON(), nullable=False, server_default=watch_default),
    )
    op.add_column(
        "instances",
        sa.Column("execution_strategy", sa.String(length=128), nullable=False, server_default=str_empty),
    )
    op.add_column(
        "instances",
        sa.Column("account_id", sa.String(length=128), nullable=False, server_default=str_empty),
    )
    op.add_column(
        "instances",
        sa.Column("model_id", sa.String(length=128), nullable=False, server_default=str_empty),
    )
    op.add_column(
        "instances",
        sa.Column("settings", sa.JSON(), nullable=True),
    )

    inspector = sa.inspect(bind)
    if "agent_configs" in inspector.get_table_names():
        rows = bind.execute(
            sa.text(
                "SELECT instance_id, watch_symbols, execution_strategy, account_id, model_id, settings, updated_at "
                "FROM agent_configs"
            )
        ).mappings().all()
        for row in rows:
            bind.execute(
                sa.text(
                    "UPDATE instances SET watch_symbols = :ws, execution_strategy = :es, "
                    "account_id = :aid, model_id = :mid, settings = :st, updated_at = :ua "
                    "WHERE instance_id = :iid"
                ),
                {
                    "ws": row["watch_symbols"],
                    "es": row["execution_strategy"],
                    "aid": row["account_id"],
                    "mid": row["model_id"],
                    "st": row["settings"],
                    "ua": row["updated_at"],
                    "iid": row["instance_id"],
                },
            )
        op.drop_table("agent_configs")


def downgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "agent_configs",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("watch_symbols", sa.JSON(), nullable=False),
        sa.Column("execution_strategy", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instance_id"),
    )

    rows = bind.execute(
        sa.text(
            "SELECT instance_id, watch_symbols, execution_strategy, account_id, model_id, settings, "
            "created_at, updated_at FROM instances"
        )
    ).mappings().all()
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO agent_configs (instance_id, watch_symbols, execution_strategy, account_id, "
                "model_id, settings, created_at, updated_at) VALUES "
                "(:iid, :ws, :es, :aid, :mid, :st, :ca, :ua)"
            ),
            {
                "iid": row["instance_id"],
                "ws": row["watch_symbols"],
                "es": row["execution_strategy"],
                "aid": row["account_id"],
                "mid": row["model_id"],
                "st": row["settings"],
                "ca": row["created_at"],
                "ua": row["updated_at"],
            },
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("instances") as batch:
            batch.drop_column("settings")
            batch.drop_column("model_id")
            batch.drop_column("account_id")
            batch.drop_column("execution_strategy")
            batch.drop_column("watch_symbols")
    else:
        op.drop_column("instances", "settings")
        op.drop_column("instances", "model_id")
        op.drop_column("instances", "account_id")
        op.drop_column("instances", "execution_strategy")
        op.drop_column("instances", "watch_symbols")
