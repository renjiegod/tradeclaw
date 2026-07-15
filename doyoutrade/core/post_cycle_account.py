from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from doyoutrade.core.models import AccountSnapshot, PositionSnapshot
from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str
from doyoutrade.core.share_math import floor_whole_share_count

_ZERO_QTY_EPS = 1e-12


def _position_row(
    p: PositionSnapshot,
    *,
    source: str,
    symbol_to_price: dict[str, float] | None,
) -> dict[str, Any] | None:
    qty_i = floor_whole_share_count(float(p.quantity))
    if qty_i <= 0:
        return None

    last_price = p.market_price
    if last_price is None and symbol_to_price:
        last_price = symbol_to_price.get(p.symbol)

    if last_price is not None:
        market_value = decimal_to_json_str(Decimal(qty_i) * decimal_from_number(last_price))
    elif p.market_value is not None:
        market_value = decimal_to_json_str(decimal_from_number(p.market_value))
    else:
        market_value = None

    if p.available is not None:
        available_i: int | None = floor_whole_share_count(float(p.available))
    elif source == "ledger":
        available_i = 0
    else:
        available_i = None

    last_price_s: str | None
    if last_price is not None:
        last_price_s = decimal_to_json_str(decimal_from_number(last_price))
    else:
        last_price_s = None

    return {
        "symbol": p.symbol,
        "name": p.name,
        "quantity": qty_i,
        "available": available_i,
        "cost_price": decimal_to_json_str(p.cost_price),
        "last_price": last_price_s,
        "market_value": market_value,
        "frozen": floor_whole_share_count(float(p.frozen)) if p.frozen is not None else None,
    }


def build_post_cycle_account(
    *,
    account: AccountSnapshot,
    positions: list[PositionSnapshot],
    source: str,
    symbol_to_price: dict[str, float] | None = None,
    captured_at: datetime | None = None,
    account_reader_class: str | None = None,
    data_provider_class: str | None = None,
) -> dict[str, Any]:
    """Build ``details['post_cycle_account']`` for ``cycle_runs`` (spec 2026-04-18).

    Monetary fields are **decimal strings** (full precision, no rounding).
    """
    ts = captured_at or datetime.now(timezone.utc)
    iso = ts.isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    elif iso.endswith("-00:00"):
        iso = iso[:-6] + "Z"

    # Drop zero-size lines: mock ledger keeps a placeholder (e.g. 600000.SH qty 0); brokers may omit these.
    held = [p for p in positions if abs(float(p.quantity)) > _ZERO_QTY_EPS]
    rows: list[dict[str, Any]] = []
    for p in held:
        row = _position_row(p, source=source, symbol_to_price=symbol_to_price)
        if row is not None:
            rows.append(row)
    total_mv = Decimal(0)
    for r in rows:
        mv_raw = r.get("market_value")
        if mv_raw is not None:
            total_mv += decimal_from_number(mv_raw)
    result = {
        "source": source,
        "captured_at": iso,
        "account": {
            "cash": decimal_to_json_str(account.cash),
            "equity": decimal_to_json_str(account.equity),
        },
        "total_market_value": decimal_to_json_str(total_mv),
        "positions": rows,
    }
    if account_reader_class is not None:
        result["account_reader_class"] = account_reader_class
    if data_provider_class is not None:
        result["data_provider_class"] = data_provider_class
    return result
