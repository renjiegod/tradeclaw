from __future__ import annotations

from qmt_proxy_sdk.data import DataApi
from qmt_proxy_sdk.http import AsyncHttpTransport
from qmt_proxy_sdk.system import SystemApi
from qmt_proxy_sdk.trading import TradingApi


class AsyncQmtProxyClient:
    """Async-first client for the qmt-proxy REST / WebSocket API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
        transport: AsyncHttpTransport | None = None,
    ) -> None:
        if transport is None:
            transport = AsyncHttpTransport(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                headers=headers,
            )
            self._owns_transport = True
        else:
            self._owns_transport = False

        self._transport = transport
        self.data = DataApi(self._transport)
        self.system = SystemApi(self._transport)
        self.trading = TradingApi(self._transport)

    async def __aenter__(self) -> AsyncQmtProxyClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        return await self._transport.request(method, path, **kwargs)

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()
