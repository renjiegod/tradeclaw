from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, List

from doyoutrade.data.instrumentation import data_span
from doyoutrade.core.models import (
    AccountSnapshot,
    AssetSnapshot,
    PositionSnapshot,
    TradeSnapshot,
)

if TYPE_CHECKING:
    from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient


class QmtAccountReader:
    """Maps qmt-proxy account/positions into domain snapshots.

    The :meth:`get_account_snapshot` / :meth:`get_positions` pair satisfies the
    core ``AccountReader`` protocol used by the worker cycle. The additional
    :meth:`get_asset_snapshot` / :meth:`get_trades` methods are NOT part of that
    protocol — they exist for the daily-review statement, which constructs this
    concrete reader directly, so the worker's snapshot+positions contract is
    untouched.
    """

    portfolio_source: str = "broker"

    def __init__(self, client: "QmtProxyRestClient"):
        self.client = client

    async def get_account_snapshot(self) -> AccountSnapshot:
        with data_span("qmt", "get_account_snapshot"):
            account = await self.client.fetch_account()
            return AccountSnapshot(cash=account["cash"], equity=account["equity"])

    async def get_positions(self) -> List[PositionSnapshot]:
        with data_span("qmt", "get_positions"):
            rows = await self.client.fetch_positions()
            positions: List[PositionSnapshot] = []
            for row in rows:
                name = row.get("name")
                positions.append(
                    PositionSnapshot(
                        symbol=row["symbol"],
                        quantity=float(row["quantity"]),
                        cost_price=row["cost_price"],
                        available=float(row["available"]) if row.get("available") is not None else None,
                        market_price=float(row["market_price"]) if row.get("market_price") is not None else None,
                        market_value=float(row["market_value"]) if row.get("market_value") is not None else None,
                        name=str(name) if name else None,
                        frozen=float(row["frozen"]) if row.get("frozen") is not None else None,
                    )
                )
            return positions

    async def get_asset_snapshot(self) -> AssetSnapshot:
        with data_span("qmt", "get_asset_snapshot"):
            asset = await self.client.fetch_asset()
            return AssetSnapshot(
                total_asset=asset["total_asset"],
                market_value=asset["market_value"],
                cash=asset["cash"],
                frozen_cash=asset["frozen_cash"],
                available_cash=asset["available_cash"],
                profit_loss=asset["profit_loss"],
                profit_loss_ratio=float(asset["profit_loss_ratio"]),
            )

    async def get_trades(self, asof: date | None = None) -> List[TradeSnapshot]:
        """Return executed broker trades, optionally filtered to one ``asof`` date.

        qmt-proxy ``get_trades`` returns the live session's trades (typically the
        current trading day); ``asof`` filters by the ISO date prefix of
        ``trade_time`` so an after-close review gets exactly that day's 交割单.
        """
        with data_span("qmt", "get_trades"):
            rows = await self.client.fetch_trades()
            asof_iso = asof.isoformat() if asof is not None else None
            trades: List[TradeSnapshot] = []
            for row in rows:
                trade_time = str(row["trade_time"])
                if asof_iso is not None and trade_time[:10] != asof_iso:
                    continue
                trades.append(
                    TradeSnapshot(
                        trade_id=str(row["trade_id"]),
                        order_id=str(row["order_id"]),
                        symbol=row["symbol"],
                        side=row["side"],
                        quantity=int(row["quantity"]),
                        price=row["price"],
                        amount=row["amount"],
                        trade_time=trade_time,
                        commission=row["commission"],
                    )
                )
            return trades
