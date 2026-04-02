from __future__ import annotations

from typing import Optional

from qmt_proxy_sdk import AsyncQmtProxyClient


class QmtProxyRestClient:
    """Async adapter over vendored qmt_proxy_sdk APIs."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout_seconds: float = 5.0,
        session_id: Optional[str] = None,
        sdk_client: AsyncQmtProxyClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self.session_id = session_id
        self._owns_client = sdk_client is None
        self._client = sdk_client or AsyncQmtProxyClient(
            base_url=self.base_url,
            api_key=token,
            timeout=self.timeout_seconds,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()
            return
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()

    async def check_health(self):
        return await self._client.system.check_health()

    async def fetch_history(self, symbol: str, start_time: str, end_time: str, interval: str = "1m"):
        payload = await self._client.data.get_market_data(
            stock_codes=[symbol],
            start_date=_compact_date(start_time),
            end_date=_compact_date(end_time),
            period=interval,
            fields=["time", "open", "high", "low", "close", "volume"],
        )
        rows = []
        for item in payload:
            for row in item.data:
                rows.append(
                    {
                        "symbol": item.stock_code,
                        "ts": row.get("time") or row.get("timestamp") or start_time,
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume", 0),
                    }
                )
        return rows

    async def fetch_account(self):
        session_id = self._require_session_id()
        account = await self._client.trading.get_account_info(session_id)
        return {
            "account_id": account.account_id,
            "cash": float(account.available_balance),
            "equity": float(account.total_asset),
            "balance": float(account.balance),
            "market_value": float(account.market_value),
            "status": account.status,
        }

    async def fetch_positions(self):
        session_id = self._require_session_id()
        positions = await self._client.trading.get_positions(session_id)
        return [
            {
                "symbol": item.stock_code,
                "quantity": float(item.volume),
                "cost_price": float(item.cost_price),
                "market_price": float(item.market_price),
                "market_value": float(item.market_value),
                "profit_loss": float(item.profit_loss),
            }
            for item in positions
        ]

    async def fetch_latest_quotes(self, symbols):
        response = await self._client.data.get_full_tick(stock_codes=list(symbols))
        quotes = []
        for symbol in symbols:
            ticks = response.ticks.get(symbol, [])
            if not ticks:
                continue
            tick = ticks[-1]
            price = float(tick.last_price)
            quotes.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "last": price,
                    "ts": tick.time,
                }
            )
        return quotes

    def _require_session_id(self) -> str:
        if not self.session_id:
            raise RuntimeError("qmt session_id is required for trading account queries")
        return self.session_id


class QmtProxyWsClient:
    """Thin async WebSocket wrapper over the vendored SDK."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        sdk_client: AsyncQmtProxyClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_client = sdk_client is None
        self._client = sdk_client or AsyncQmtProxyClient(
            base_url=self.base_url,
            api_key=token,
        )

    def subscribe_quotes(self, symbols, period: str = "tick"):
        return self._client.data.subscribe_and_stream(symbols=list(symbols), period=period)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()


def _compact_date(value: str) -> str:
    if not value:
        return ""
    date_part = value.split("T", 1)[0]
    return date_part.replace("-", "")
