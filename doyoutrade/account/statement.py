"""Aggregate a live broker account statement for the daily review.

Gathers, for one ``asof`` trading day, the cash/equity snapshot + positions,
the richer asset breakdown, and the day's executed trades (交割单) from a
:class:`~doyoutrade.account.qmt_reader.QmtAccountReader`-shaped reader. Every
monetary field is serialized as a full-precision decimal STRING (never
float-rounded) — the same money contract as
:func:`doyoutrade.core.post_cycle_account.build_post_cycle_account`, which is
reused verbatim for the account+positions block.

Per CLAUDE.md §错误可见性 each sub-fetch is independently guarded: a failure on
one surface (e.g. trades) records a structured ``errors`` entry AND logs with
type+message, but never silently drops the others — so a half-fetched statement
is never mistaken for a complete one downstream.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from doyoutrade.core.models import AssetSnapshot, TradeSnapshot
from doyoutrade.core.post_cycle_account import build_post_cycle_account
from doyoutrade.money.decimal_helpers import decimal_to_json_str

logger = logging.getLogger(__name__)


def _asset_to_json(asset: AssetSnapshot) -> dict[str, Any]:
    return {
        "total_asset": decimal_to_json_str(asset.total_asset),
        "market_value": decimal_to_json_str(asset.market_value),
        "cash": decimal_to_json_str(asset.cash),
        "frozen_cash": decimal_to_json_str(asset.frozen_cash),
        "available_cash": decimal_to_json_str(asset.available_cash),
        "profit_loss": decimal_to_json_str(asset.profit_loss),
        # ratio is a plain fraction from the broker, kept numeric (not money).
        "profit_loss_ratio": asset.profit_loss_ratio,
    }


def _trade_to_json(t: TradeSnapshot) -> dict[str, Any]:
    return {
        "trade_id": t.trade_id,
        "order_id": t.order_id,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "price": decimal_to_json_str(t.price),
        "amount": decimal_to_json_str(t.amount),
        "trade_time": t.trade_time,
        "commission": decimal_to_json_str(t.commission),
    }


def _record_error(
    errors: list[dict[str, str]], *, stage: str, exc: Exception, hint: str
) -> None:
    logger.warning(
        "daily_review statement: %s fetch failed (%s): %s",
        stage,
        type(exc).__name__,
        exc,
    )
    errors.append(
        {
            "stage": stage,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "hint": hint,
        }
    )


async def gather_account_statement(
    reader: Any,
    *,
    asof: date,
    captured_at: datetime | None = None,
    source: str = "broker",
) -> dict[str, Any]:
    """Build the live-account portion of the daily-review ``pre_data``.

    ``reader`` exposes ``get_account_snapshot`` / ``get_positions`` and
    (optionally) ``get_asset_snapshot`` / ``get_trades`` — the latter two are
    feature-detected so readers that lack them (mock / zero) degrade gracefully
    rather than raising.

    Returns::

        {
          "asof": "YYYY-MM-DD",
          "source": "broker",
          "account": {source, captured_at, account:{cash,equity},
                      total_market_value, positions:[...]} | None,
          "asset": {total_asset, market_value, cash, frozen_cash,
                    available_cash, profit_loss, profit_loss_ratio} | None,
          "trades": [{trade_id, order_id, symbol, side, quantity, price,
                      amount, trade_time, commission}],
          "trade_count": int,
          "errors": [{stage, error_type, message, hint}],
        }
    """
    captured = captured_at or datetime.now(timezone.utc)
    errors: list[dict[str, str]] = []

    account_snap = None
    try:
        account_snap = await reader.get_account_snapshot()
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        _record_error(
            errors,
            stage="account",
            exc=exc,
            hint="QMT account snapshot read failed; verify qmt-proxy trading "
            "session / account connection before the next review fire",
        )

    position_snaps: list = []
    try:
        position_snaps = await reader.get_positions()
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        _record_error(
            errors,
            stage="positions",
            exc=exc,
            hint="QMT positions read failed; the review will lack holdings detail",
        )

    account_block = None
    if account_snap is not None:
        account_block = build_post_cycle_account(
            account=account_snap,
            positions=position_snaps,
            source=source,
            captured_at=captured,
        )

    asset_block = None
    get_asset = getattr(reader, "get_asset_snapshot", None)
    if callable(get_asset):
        try:
            asset_block = _asset_to_json(await get_asset())
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            _record_error(
                errors,
                stage="asset",
                exc=exc,
                hint="QMT asset breakdown read failed; cash/equity from the "
                "account block is still usable",
            )

    trades_block: list[dict[str, Any]] = []
    get_trades = getattr(reader, "get_trades", None)
    if callable(get_trades):
        try:
            trade_snaps = await get_trades(asof)
            trades_block = [_trade_to_json(t) for t in trade_snaps]
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            _record_error(
                errors,
                stage="trades",
                exc=exc,
                hint="QMT executed-trades read failed; fall back to the "
                "broker-exported CSV in knowledge/trades/ for authoritative 成交",
            )

    return {
        "asof": asof.isoformat(),
        "source": source,
        "account": account_block,
        "asset": asset_block,
        "trades": trades_block,
        "trade_count": len(trades_block),
        "errors": errors,
    }
