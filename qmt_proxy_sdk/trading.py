from __future__ import annotations

from qmt_proxy_sdk.models.trading import (
    AccountInfo,
    AssetInfo,
    ConnectResponse,
    ConnectionStatus,
    OperationResult,
    OrderResponse,
    PositionInfo,
    RiskInfo,
    StrategyInfo,
    TradeInfo,
)


class TradingApi:
    def __init__(self, transport) -> None:
        self._transport = transport

    async def connect(
        self,
        *,
        account_id: str,
        password: str | None = None,
        client_id: int | None = None,
    ) -> ConnectResponse:
        payload_json = {"account_id": account_id}
        if password is not None:
            payload_json["password"] = password
        if client_id is not None:
            payload_json["client_id"] = client_id

        payload = await self._transport.request(
            "POST",
            "/api/v1/trading/connect",
            json=payload_json,
        )
        return ConnectResponse.model_validate(payload)

    async def disconnect(self, *, session_id: str) -> OperationResult:
        payload = await self._transport.request("POST", f"/api/v1/trading/disconnect/{session_id}")
        return OperationResult.model_validate(payload)

    async def get_account_info(self, session_id: str) -> AccountInfo:
        payload = await self._transport.request("GET", f"/api/v1/trading/account/{session_id}")
        return AccountInfo.model_validate(payload)

    async def get_positions(self, session_id: str) -> list[PositionInfo]:
        payload = await self._transport.request("GET", f"/api/v1/trading/positions/{session_id}")
        return [PositionInfo.model_validate(item) for item in payload]

    async def get_asset(self, session_id: str) -> AssetInfo:
        payload = await self._transport.request("GET", f"/api/v1/trading/asset/{session_id}")
        return AssetInfo.model_validate(payload)

    async def get_risk(self, session_id: str) -> RiskInfo:
        payload = await self._transport.request("GET", f"/api/v1/trading/risk/{session_id}")
        return RiskInfo.model_validate(payload)

    async def get_strategies(self, session_id: str) -> list[StrategyInfo]:
        payload = await self._transport.request("GET", f"/api/v1/trading/strategies/{session_id}")
        return [StrategyInfo.model_validate(item) for item in payload]

    async def get_orders(self, session_id: str) -> list[OrderResponse]:
        payload = await self._transport.request("GET", f"/api/v1/trading/orders/{session_id}")
        return [OrderResponse.model_validate(item) for item in payload]

    async def get_trades(self, session_id: str) -> list[TradeInfo]:
        payload = await self._transport.request("GET", f"/api/v1/trading/trades/{session_id}")
        return [TradeInfo.model_validate(item) for item in payload]

    async def submit_order(
        self,
        *,
        session_id: str,
        stock_code: str,
        side: str,
        volume: int,
        price: float | None = None,
        order_type: str = "LIMIT",
        strategy_name: str | None = None,
    ) -> OrderResponse:
        payload = await self._transport.request(
            "POST",
            f"/api/v1/trading/order/{session_id}",
            json={
                "stock_code": stock_code,
                "side": side,
                "order_type": order_type,
                "volume": volume,
                "price": price,
                "strategy_name": strategy_name,
            },
        )
        return OrderResponse.model_validate(payload)

    async def cancel_order(self, *, session_id: str, order_id: str) -> OperationResult:
        payload = await self._transport.request(
            "POST",
            f"/api/v1/trading/cancel/{session_id}",
            json={"order_id": order_id},
        )
        return OperationResult.model_validate(payload)

    async def get_connection_status(self, *, session_id: str) -> ConnectionStatus:
        payload = await self._transport.request("GET", f"/api/v1/trading/status/{session_id}")
        return ConnectionStatus.model_validate(payload)
