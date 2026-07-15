"""Merge cycle_runs universe/proposals/reviews JSON columns into details.

Revision ID: 20260416_01
Revises: 20260415_01
Create Date: 2026-04-16
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260416_01"
down_revision = "20260415_01"
branch_labels = None
depends_on = None


def _json_value(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _build_details_row(universe_json: Any, proposals_json: Any, reviews_json: Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    u = _json_value(universe_json)
    details["universe"] = [] if u is None else list(u)
    p = _json_value(proposals_json)
    if p is not None:
        details["proposals"] = list(p) if isinstance(p, list) else p
    r = _json_value(reviews_json)
    if r is not None:
        details["reviews"] = list(r) if isinstance(r, list) else r
    return details


def _backfill_details() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT run_id, universe_json, proposals_json, reviews_json FROM cycle_runs")
    ).fetchall()
    for row in rows:
        run_id = row[0]
        details = _build_details_row(row[1], row[2], row[3])
        conn.execute(
            sa.text("UPDATE cycle_runs SET details = :det WHERE run_id = :rid"),
            {"det": json.dumps(details), "rid": run_id},
        )


def _split_details_to_legacy(details_raw: Any) -> tuple[Any, Any, Any]:
    d = _json_value(details_raw)
    if not isinstance(d, dict):
        return [], None, None
    u = d.get("universe")
    universe_json = [] if u is None else list(u)
    proposals_json = d.get("proposals", None)
    if proposals_json is not None and isinstance(proposals_json, list):
        proposals_json = list(proposals_json)
    reviews_json = d.get("reviews", None)
    if reviews_json is not None and isinstance(reviews_json, list):
        reviews_json = list(reviews_json)
    return universe_json, proposals_json, reviews_json


def _backfill_legacy_columns() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT run_id, details FROM cycle_runs")).fetchall()
    for row in rows:
        run_id = row[0]
        u, p, r = _split_details_to_legacy(row[1])
        conn.execute(
            sa.text(
                "UPDATE cycle_runs SET universe_json = :u, proposals_json = :p, reviews_json = :r "
                "WHERE run_id = :rid"
            ),
            {
                "u": json.dumps(u),
                "p": None if p is None else json.dumps(p),
                "r": None if r is None else json.dumps(r),
                "rid": run_id,
            },
        )


def upgrade() -> None:
    op.add_column("cycle_runs", sa.Column("details", sa.JSON(), nullable=True))
    _backfill_details()
    op.drop_column("cycle_runs", "universe_json")
    op.drop_column("cycle_runs", "proposals_json")
    op.drop_column("cycle_runs", "reviews_json")


def downgrade() -> None:
    op.add_column("cycle_runs", sa.Column("universe_json", sa.JSON(), nullable=True))
    op.add_column("cycle_runs", sa.Column("proposals_json", sa.JSON(), nullable=True))
    op.add_column("cycle_runs", sa.Column("reviews_json", sa.JSON(), nullable=True))
    _backfill_legacy_columns()
    op.drop_column("cycle_runs", "details")
