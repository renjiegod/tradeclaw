from __future__ import annotations

from typing import TYPE_CHECKING

from qmt_proxy_sdk.models.data import (
    ConvertibleBondInfo,
    DataDirResponse,
    DividendFactor,
    DownloadResult,
    ETFInfoResponse,
    FinancialDataResponse,
    FullTickResponse,
    HolidayInfo,
    IndexWeightResponse,
    InstrumentInfo,
    InstrumentTypeInfo,
    IpoInfo,
    L2OrderData,
    L2OrderResponse,
    L2QuoteData,
    L2QuoteResponse,
    L2TransactionData,
    L2TransactionResponse,
    MarketDataResponse,
    PeriodListResponse,
    SectorOperationResult,
    SectorResponse,
    SubscriptionCreateResult,
    SubscriptionDeleteResult,
    SubscriptionInfo,
    SubscriptionListResult,
    TickData,
    TradingCalendarResponse,
)

if TYPE_CHECKING:
    from qmt_proxy_sdk.ws import QuoteStream


class DataApi:
    def __init__(self, transport) -> None:
        self._transport = transport

    # ------------------------------------------------------------------
    # WebSocket 行情流
    # ------------------------------------------------------------------

    def subscribe_and_stream(
        self,
        *,
        symbols: list[str],
        period: str = "tick",
        start_date: str = "",
        adjust_type: str = "none",
        subscription_type: str = "quote",
        heartbeat_interval: float = 30.0,
        reconnect_attempts: int = 5,
        reconnect_delay: float = 1.0,
    ) -> QuoteStream:
        """创建订阅并通过 WebSocket 流式接收实时行情。

        返回一个 :class:`QuoteStream`，同时支持 ``async for`` 和
        ``async with`` 两种用法。退出迭代后会自动清理订阅。

        Example::

            async for quote in client.data.subscribe_and_stream(
                symbols=["000001.SZ", "600000.SH"],
            ):
                print(quote.stock_code, quote.last_price)
        """
        from qmt_proxy_sdk.ws import QuoteStream

        base_url: str = str(self._transport._client.base_url).rstrip("/")
        ws_base_url = base_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )

        headers: dict[str, str] = {}
        auth = self._transport._client.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth

        return QuoteStream(
            data_api=self,
            ws_base_url=ws_base_url,
            symbols=symbols,
            period=period,
            start_date=start_date,
            adjust_type=adjust_type,
            subscription_type=subscription_type,
            headers=headers,
            heartbeat_interval=heartbeat_interval,
            reconnect_attempts=reconnect_attempts,
            reconnect_delay=reconnect_delay,
        )

    # ------------------------------------------------------------------
    # 市场数据
    # ------------------------------------------------------------------

    async def get_market_data(
        self,
        *,
        stock_codes: list[str],
        start_date: str = "",
        end_date: str = "",
        period: str = "1d",
        fields: list[str] | None = None,
        adjust_type: str = "none",
        fill_data: bool = True,
        disable_download: bool = False,
    ) -> list[MarketDataResponse]:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/market",
            json={
                "stock_codes": stock_codes,
                "start_date": start_date,
                "end_date": end_date,
                "period": period,
                "fields": fields,
                "adjust_type": adjust_type,
                "fill_data": fill_data,
                "disable_download": disable_download,
            },
        )
        return [MarketDataResponse.model_validate(item) for item in payload]

    async def get_financial_data(
        self,
        *,
        stock_codes: list[str],
        table_list: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[FinancialDataResponse]:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/financial",
            json={
                "stock_codes": stock_codes,
                "table_list": table_list,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        return [FinancialDataResponse.model_validate(item) for item in payload]

    async def get_sector_list(self) -> list[SectorResponse]:
        payload = await self._transport.request("GET", "/api/v1/data/sectors")
        return [SectorResponse.model_validate(item) for item in payload]

    async def get_stock_list_in_sector(
        self, sector_name: str, sector_type: str | None = None
    ) -> SectorResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector",
            json={"sector_name": sector_name, "sector_type": sector_type},
        )
        return SectorResponse.model_validate(payload)

    async def get_index_weight(
        self, index_code: str, *, date: str | None = None
    ) -> IndexWeightResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/index-weight",
            json={"index_code": index_code, "date": date},
        )
        return IndexWeightResponse.model_validate(payload)

    async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
        payload = await self._transport.request(
            "GET", f"/api/v1/data/trading-calendar/{year}"
        )
        return TradingCalendarResponse.model_validate(payload)

    async def get_instrument_info(self, stock_code: str) -> InstrumentInfo:
        payload = await self._transport.request(
            "GET", f"/api/v1/data/instrument/{stock_code}"
        )
        return InstrumentInfo.model_validate(payload)

    async def get_etf_info(self, etf_code: str) -> ETFInfoResponse:
        payload = await self._transport.request(
            "GET", f"/api/v1/data/etf/{etf_code}"
        )
        return ETFInfoResponse.model_validate(payload)

    async def get_instrument_type(self, *, stock_code: str) -> InstrumentTypeInfo:
        payload = await self._transport.request(
            "GET", f"/api/v1/data/instrument-type/{stock_code}"
        )
        return InstrumentTypeInfo.model_validate(payload)

    async def get_holidays(self) -> HolidayInfo:
        payload = await self._transport.request("GET", "/api/v1/data/holidays")
        return HolidayInfo.model_validate(payload)

    async def get_convertible_bonds(self) -> list[ConvertibleBondInfo]:
        payload = await self._transport.request(
            "GET", "/api/v1/data/convertible-bonds"
        )
        return [ConvertibleBondInfo.model_validate(item) for item in payload]

    async def get_ipo_info(self) -> list[IpoInfo]:
        payload = await self._transport.request("GET", "/api/v1/data/ipo-info")
        return [IpoInfo.model_validate(item) for item in payload]

    async def get_period_list(self) -> PeriodListResponse:
        payload = await self._transport.request("GET", "/api/v1/data/period-list")
        return PeriodListResponse.model_validate(payload)

    async def get_data_dir(self) -> DataDirResponse:
        payload = await self._transport.request("GET", "/api/v1/data/data-dir")
        return DataDirResponse.model_validate(payload)

    # ------------------------------------------------------------------
    # 本地数据 / Tick / K线 / 除权
    # ------------------------------------------------------------------

    async def get_local_data(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
        period: str = "1d",
        fields: list[str] | None = None,
        adjust_type: str = "none",
    ) -> list[MarketDataResponse]:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/local-data",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
                "period": period,
                "fields": fields,
                "adjust_type": adjust_type,
            },
        )
        return [MarketDataResponse.model_validate(item) for item in payload]

    async def get_full_tick(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
    ) -> FullTickResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/full-tick",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        ticks = {
            code: [TickData.model_validate(t) for t in items]
            for code, items in payload.items()
        }
        return FullTickResponse(ticks=ticks)

    async def get_divid_factors(self, *, stock_code: str) -> list[DividendFactor]:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/divid-factors",
            json={"stock_code": stock_code},
        )
        return [DividendFactor.model_validate(item) for item in payload]

    async def get_full_kline(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
        period: str = "1d",
        fields: list[str] | None = None,
        adjust_type: str = "none",
    ) -> list[MarketDataResponse]:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/full-kline",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
                "period": period,
                "fields": fields,
                "adjust_type": adjust_type,
            },
        )
        return [MarketDataResponse.model_validate(item) for item in payload]

    # ------------------------------------------------------------------
    # 数据下载
    # ------------------------------------------------------------------

    async def download_history_data(
        self,
        *,
        stock_code: str,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        incrementally: bool = False,
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/history-data",
            json={
                "stock_code": stock_code,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "incrementally": incrementally,
            },
        )
        return DownloadResult.model_validate(payload)

    async def download_history_data_batch(
        self,
        *,
        stock_list: list[str],
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/history-data-batch",
            json={
                "stock_list": stock_list,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        return DownloadResult.model_validate(payload)

    async def download_financial_data(
        self,
        *,
        stock_list: list[str],
        table_list: list[str],
        start_date: str = "",
        end_date: str = "",
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/financial-data",
            json={
                "stock_list": stock_list,
                "table_list": table_list,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        return DownloadResult.model_validate(payload)

    async def download_financial_data_batch(
        self,
        *,
        stock_list: list[str],
        table_list: list[str],
        start_date: str = "",
        end_date: str = "",
        callback_func: str | None = None,
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/financial-data-batch",
            json={
                "stock_list": stock_list,
                "table_list": table_list,
                "start_date": start_date,
                "end_date": end_date,
                "callback_func": callback_func,
            },
        )
        return DownloadResult.model_validate(payload)

    async def download_sector_data(self) -> DownloadResult:
        payload = await self._transport.request(
            "POST", "/api/v1/data/download/sector-data"
        )
        return DownloadResult.model_validate(payload)

    async def download_index_weight(
        self, *, index_code: str | None = None
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/index-weight",
            json={"index_code": index_code},
        )
        return DownloadResult.model_validate(payload)

    async def download_cb_data(self) -> DownloadResult:
        payload = await self._transport.request(
            "POST", "/api/v1/data/download/cb-data"
        )
        return DownloadResult.model_validate(payload)

    async def download_etf_info(self) -> DownloadResult:
        payload = await self._transport.request(
            "POST", "/api/v1/data/download/etf-info"
        )
        return DownloadResult.model_validate(payload)

    async def download_holiday_data(self) -> DownloadResult:
        payload = await self._transport.request(
            "POST", "/api/v1/data/download/holiday-data"
        )
        return DownloadResult.model_validate(payload)

    async def download_history_contracts(
        self, *, market: str | None = None
    ) -> DownloadResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/download/history-contracts",
            json={"market": market},
        )
        return DownloadResult.model_validate(payload)

    # ------------------------------------------------------------------
    # 板块管理
    # ------------------------------------------------------------------

    async def create_sector_folder(
        self, *, parent_node: str = "", folder_name: str = ""
    ) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/create-folder",
            params={"parent_node": parent_node, "folder_name": folder_name},
        )
        return SectorOperationResult.model_validate(payload)

    async def create_sector(
        self,
        *,
        sector_name: str,
        parent_node: str = "",
        overwrite: bool = True,
    ) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/create",
            json={
                "parent_node": parent_node,
                "sector_name": sector_name,
                "overwrite": overwrite,
            },
        )
        return SectorOperationResult.model_validate(payload)

    async def add_sector_stocks(
        self, *, sector_name: str, stock_list: list[str]
    ) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/add-stocks",
            json={"sector_name": sector_name, "stock_list": stock_list},
        )
        return SectorOperationResult.model_validate(payload or {})

    async def remove_sector_stocks(
        self, *, sector_name: str, stock_list: list[str]
    ) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/remove-stocks",
            json={"sector_name": sector_name, "stock_list": stock_list},
        )
        return SectorOperationResult.model_validate(payload or {})

    async def remove_sector(self, *, sector_name: str) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/remove",
            params={"sector_name": sector_name},
        )
        return SectorOperationResult.model_validate(payload or {})

    async def reset_sector(
        self, *, sector_name: str, stock_list: list[str]
    ) -> SectorOperationResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/sector/reset",
            json={"sector_name": sector_name, "stock_list": stock_list},
        )
        return SectorOperationResult.model_validate(payload or {})

    # ------------------------------------------------------------------
    # Level-2 数据
    # ------------------------------------------------------------------

    async def get_l2_quote(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
    ) -> L2QuoteResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/l2/quote",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        quotes = {
            code: L2QuoteData.model_validate(data)
            for code, data in payload.items()
        }
        return L2QuoteResponse(quotes=quotes)

    async def get_l2_order(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
    ) -> L2OrderResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/l2/order",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        orders = {
            code: [L2OrderData.model_validate(o) for o in items]
            for code, items in payload.items()
        }
        return L2OrderResponse(orders=orders)

    async def get_l2_transaction(
        self,
        *,
        stock_codes: list[str],
        start_time: str = "",
        end_time: str = "",
    ) -> L2TransactionResponse:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/l2/transaction",
            json={
                "stock_codes": stock_codes,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        transactions = {
            code: [L2TransactionData.model_validate(t) for t in items]
            for code, items in payload.items()
        }
        return L2TransactionResponse(transactions=transactions)

    # ------------------------------------------------------------------
    # 订阅管理（REST）
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        *,
        symbols: list[str],
        period: str = "tick",
        start_date: str = "",
        adjust_type: str = "none",
        subscription_type: str = "quote",
    ) -> SubscriptionCreateResult:
        payload = await self._transport.request(
            "POST",
            "/api/v1/data/subscription",
            json={
                "symbols": symbols,
                "period": period,
                "start_date": start_date,
                "adjust_type": adjust_type,
                "subscription_type": subscription_type,
            },
        )
        return SubscriptionCreateResult.model_validate(payload)

    async def delete_subscription(
        self, *, subscription_id: str
    ) -> SubscriptionDeleteResult:
        payload = await self._transport.request(
            "DELETE", f"/api/v1/data/subscription/{subscription_id}"
        )
        return SubscriptionDeleteResult.model_validate(payload)

    async def get_subscription(
        self, *, subscription_id: str
    ) -> SubscriptionInfo:
        payload = await self._transport.request(
            "GET", f"/api/v1/data/subscription/{subscription_id}"
        )
        return SubscriptionInfo.model_validate(payload)

    async def list_subscriptions(self) -> SubscriptionListResult:
        payload = await self._transport.request(
            "GET", "/api/v1/data/subscriptions"
        )
        return SubscriptionListResult.model_validate(payload)
