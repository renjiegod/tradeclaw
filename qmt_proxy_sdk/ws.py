"""WebSocket 行情流客户端，支持自动心跳和断线重连。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

import websockets
from websockets.asyncio.client import connect

from qmt_proxy_sdk.exceptions import QmtProxyError, TransportError
from qmt_proxy_sdk.models.data import QuoteData

if TYPE_CHECKING:
    from qmt_proxy_sdk.data import DataApi

logger = logging.getLogger(__name__)


class QuoteStream:
    """WebSocket 行情流。

    典型用法::

        stream = client.data.subscribe_and_stream(symbols=["000001.SZ"])
        async with stream:
            async for quote in stream:
                print(quote.stock_code, quote.last_price)

    也可直接迭代（退出时自动清理订阅）::

        async for quote in client.data.subscribe_and_stream(symbols=["000001.SZ"]):
            ...
    """

    def __init__(
        self,
        *,
        data_api: DataApi,
        ws_base_url: str,
        symbols: list[str],
        period: str = "tick",
        start_date: str = "",
        adjust_type: str = "none",
        subscription_type: str = "quote",
        headers: dict[str, str] | None = None,
        heartbeat_interval: float = 30.0,
        reconnect_attempts: int = 5,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._data_api = data_api
        self._ws_base_url = ws_base_url
        self._symbols = symbols
        self._period = period
        self._start_date = start_date
        self._adjust_type = adjust_type
        self._subscription_type = subscription_type
        self._headers = headers or {}
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay

        self._subscription_id: str | None = None
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._closed = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> QuoteStream:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Async iterator
    # ------------------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[QuoteData]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[QuoteData]:
        try:
            subscription_id = await self._ensure_subscription()
            ws_url = f"{self._ws_base_url}/ws/quote/{subscription_id}"
            attempt = 0

            while not self._closed:
                try:
                    async with connect(
                        ws_url,
                        additional_headers=self._headers,
                    ) as ws:
                        self._ws = ws
                        attempt = 0
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(ws)
                        )

                        try:
                            async for raw in ws:
                                msg = json.loads(raw)
                                msg_type = msg.get("type")

                                if msg_type == "quote":
                                    yield self._parse_quote_message(msg)
                                elif msg_type == "error":
                                    raise QmtProxyError(
                                        msg.get("message", "Unknown WebSocket error")
                                    )
                                elif msg_type in ("connected", "pong"):
                                    continue
                        finally:
                            self._cancel_heartbeat()

                except (
                    websockets.ConnectionClosed,
                    websockets.InvalidURI,
                    ConnectionError,
                    OSError,
                ) as exc:
                    if self._closed:
                        break
                    attempt += 1
                    if attempt > self._reconnect_attempts:
                        raise TransportError(
                            f"WebSocket reconnect failed after "
                            f"{self._reconnect_attempts} attempts"
                        ) from exc
                    delay = self._reconnect_delay * attempt
                    logger.warning(
                        "WebSocket disconnected, reconnecting in %.1fs "
                        "(attempt %d/%d)",
                        delay,
                        attempt,
                        self._reconnect_attempts,
                    )
                    await asyncio.sleep(delay)
        finally:
            await self._cleanup_subscription()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse_quote_message(self, msg: dict) -> QuoteData:
        """Normalize quote payloads from both flat and xtdata-nested formats."""
        payload = msg["data"]
        if isinstance(payload, dict):
            nested_quote = self._normalize_nested_quote_payload(payload, msg)
            if nested_quote is not None:
                return QuoteData.model_validate(nested_quote)

            flat_quote = dict(payload)
            if msg.get("timestamp") and "timestamp" not in flat_quote:
                flat_quote["timestamp"] = msg["timestamp"]
            return QuoteData.model_validate(flat_quote)

        return QuoteData.model_validate(payload)

    def _normalize_nested_quote_payload(
        self, payload: dict, msg: dict
    ) -> dict | None:
        """Convert ``{symbol: [xtdata_tick]}`` payloads into QuoteData fields."""
        if len(payload) != 1:
            return None

        stock_code, raw_items = next(iter(payload.items()))
        if not isinstance(stock_code, str):
            return None
        if not isinstance(raw_items, list) or not raw_items:
            return None

        raw_quote = raw_items[0]
        if not isinstance(raw_quote, dict):
            return None

        normalized = dict(raw_quote)
        normalized.update(
            {
                "stock_code": stock_code,
                "timestamp": msg.get("timestamp", raw_quote.get("time")),
                "last_price": raw_quote.get("lastPrice"),
                "pre_close": raw_quote.get("lastClose"),
                "bid_price": raw_quote.get("bidPrice"),
                "ask_price": raw_quote.get("askPrice"),
                "bid_vol": raw_quote.get("bidVol"),
                "ask_vol": raw_quote.get("askVol"),
            }
        )
        return normalized

    async def _ensure_subscription(self) -> str:
        if self._subscription_id is None:
            result = await self._data_api.create_subscription(
                symbols=self._symbols,
                period=self._period,
                start_date=self._start_date,
                adjust_type=self._adjust_type,
                subscription_type=self._subscription_type,
            )
            self._subscription_id = result.subscription_id
            logger.info("Created subscription %s", self._subscription_id)
        return self._subscription_id

    async def _heartbeat_loop(
        self, ws: websockets.asyncio.client.ClientConnection
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send(json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            pass

    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def _cleanup_subscription(self) -> None:
        if self._subscription_id:
            sid = self._subscription_id
            self._subscription_id = None
            try:
                await self._data_api.delete_subscription(subscription_id=sid)
                logger.info("Deleted subscription %s", sid)
            except Exception:
                logger.debug("Failed to delete subscription %s", sid, exc_info=True)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Gracefully close the stream, cancel heartbeat, and delete subscription."""
        self._closed = True
        self._cancel_heartbeat()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        await self._cleanup_subscription()

    @property
    def subscription_id(self) -> str | None:
        return self._subscription_id

    @property
    def closed(self) -> bool:
        return self._closed
