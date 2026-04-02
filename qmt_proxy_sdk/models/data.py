from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MarketDataResponse(BaseModel):
    stock_code: str
    data: List[Dict[str, Any]]
    fields: List[str]
    period: str
    start_date: str
    end_date: str


class FinancialDataResponse(BaseModel):
    stock_code: str
    table_name: str
    data: List[Dict[str, Any]]
    columns: List[str]


class SectorResponse(BaseModel):
    sector_name: str
    stock_list: List[str]
    sector_type: Optional[str] = None


class IndexWeightResponse(BaseModel):
    index_code: str
    date: str
    weights: List[Dict[str, Any]]


class InstrumentInfo(BaseModel):
    ExchangeID: Optional[str] = Field(None, description="合约市场代码")
    InstrumentID: Optional[str] = Field(None, description="合约代码")
    InstrumentName: Optional[str] = Field(None, description="合约名称")
    ProductID: Optional[str] = Field(None, description="合约的品种ID(期货)")
    ProductName: Optional[str] = Field(None, description="合约的品种名称(期货)")
    ProductType: Optional[int] = Field(None, description="合约的类型")
    ExchangeCode: Optional[str] = Field(None, description="交易所代码")
    UniCode: Optional[str] = Field(None, description="统一规则代码")
    CreateDate: Optional[str] = Field(None, description="创建日期")
    OpenDate: Optional[str] = Field(None, description="上市日期")
    ExpireDate: Optional[int] = Field(None, description="退市日或者到期日")
    PreClose: Optional[float] = Field(None, description="前收盘价格")
    SettlementPrice: Optional[float] = Field(None, description="前结算价格")
    UpStopPrice: Optional[float] = Field(None, description="当日涨停价")
    DownStopPrice: Optional[float] = Field(None, description="当日跌停价")
    FloatVolume: Optional[float] = Field(None, description="流通股本")
    TotalVolume: Optional[float] = Field(None, description="总股本")
    LongMarginRatio: Optional[float] = Field(None, description="多头保证金率")
    ShortMarginRatio: Optional[float] = Field(None, description="空头保证金率")
    PriceTick: Optional[float] = Field(None, description="最小价格变动单位")
    VolumeMultiple: Optional[int] = Field(None, description="合约乘数")
    MainContract: Optional[int] = Field(None, description="主力合约标记")
    LastVolume: Optional[int] = Field(None, description="昨日持仓量")
    InstrumentStatus: Optional[int] = Field(None, description="合约停牌状态")
    IsTrading: Optional[bool] = Field(None, description="合约是否可交易")
    IsRecent: Optional[bool] = Field(None, description="是否是近月合约")
    instrument_code: Optional[str] = Field(None, description="合约代码（兼容字段）")
    instrument_name: Optional[str] = Field(None, description="合约名称（兼容字段）")
    market_type: Optional[str] = Field(None, description="市场类型（兼容字段）")
    instrument_type: Optional[str] = Field(None, description="合约类型（兼容字段）")
    list_date: Optional[str] = Field(None, description="上市日期（兼容字段）")
    delist_date: Optional[str] = Field(None, description="退市日期（兼容字段）")


class TradingCalendarResponse(BaseModel):
    trading_dates: List[str]
    holidays: List[str]
    year: int


class ETFInfoResponse(BaseModel):
    etf_code: str
    etf_name: str
    underlying_asset: str
    creation_unit: int
    redemption_unit: int


class InstrumentTypeInfo(BaseModel):
    stock_code: str = Field(..., description="合约代码")
    index: bool = Field(False, description="是否为指数")
    stock: bool = Field(False, description="是否为股票")
    fund: bool = Field(False, description="是否为基金")
    etf: bool = Field(False, description="是否为ETF")
    bond: bool = Field(False, description="是否为债券")
    option: bool = Field(False, description="是否为期权")
    futures: bool = Field(False, description="是否为期货")


class HolidayInfo(BaseModel):
    holidays: List[str] = Field(..., description="节假日列表，YYYYMMDD格式")


class ConvertibleBondInfo(BaseModel):
    bond_code: str = Field(..., description="可转债代码")
    bond_name: Optional[str] = Field(None, description="可转债名称")
    stock_code: Optional[str] = Field(None, description="正股代码")
    stock_name: Optional[str] = Field(None, description="正股名称")
    conversion_price: Optional[float] = Field(None, description="转股价格")
    conversion_value: Optional[float] = Field(None, description="转股价值")
    conversion_premium_rate: Optional[float] = Field(None, description="转股溢价率")
    current_price: Optional[float] = Field(None, description="可转债当前价格")
    par_value: Optional[float] = Field(None, description="债券面值")
    list_date: Optional[str] = Field(None, description="上市日期")
    maturity_date: Optional[str] = Field(None, description="到期日期")
    conversion_begin_date: Optional[str] = Field(None, description="转股起始日")
    conversion_end_date: Optional[str] = Field(None, description="转股结束日")
    raw_data: Optional[Dict[str, Any]] = Field(None, description="原始数据（包含所有xtdata字段）")


class IpoInfo(BaseModel):
    security_code: str = Field(..., description="证券代码")
    code_name: Optional[str] = Field(None, description="代码简称")
    market: Optional[str] = Field(None, description="所属市场")
    act_issue_qty: Optional[int] = Field(None, description="发行总量（股）")
    online_issue_qty: Optional[int] = Field(None, description="网上发行量（股）")
    online_sub_code: Optional[str] = Field(None, description="申购代码")
    online_sub_max_qty: Optional[int] = Field(None, description="申购上限（股）")
    publish_price: Optional[float] = Field(None, description="发行价格")
    is_profit: Optional[int] = Field(None, description="是否已盈利")
    industry_pe: Optional[float] = Field(None, description="行业市盈率")
    after_pe: Optional[float] = Field(None, description="发行后市盈率")
    subscribe_date: Optional[str] = Field(None, description="申购日期")
    lottery_date: Optional[str] = Field(None, description="摇号日期")
    list_date: Optional[str] = Field(None, description="上市日期")
    raw_data: Optional[Dict[str, Any]] = Field(None, description="原始数据（包含所有xtdata字段）")


class PeriodListResponse(BaseModel):
    periods: List[str] = Field(..., description="可用周期列表")


class DataDirResponse(BaseModel):
    data_dir: str = Field(..., description="本地数据路径")


# ==================== 订阅相关模型 ====================


class SubscriptionCreateResult(BaseModel):
    subscription_id: str
    status: str
    created_at: str | None = None
    symbols: list[str] | None = None
    period: str | None = None
    start_date: str | None = None
    adjust_type: str | None = None
    subscription_type: str | None = None
    message: str | None = None


class SubscriptionDeleteResult(BaseModel):
    success: bool
    message: str
    subscription_id: str


class SubscriptionInfo(BaseModel):
    subscription_id: str
    active: bool
    symbols: list[str] | None = None
    period: str | None = None
    start_date: str | None = None
    adjust_type: str | None = None
    subscription_type: str | None = None
    created_at: str | None = None
    last_heartbeat: str | None = None
    queue_size: int | None = None
    subids_xtquant: list[int] | None = None


class SubscriptionListResult(BaseModel):
    subscriptions: list[SubscriptionInfo]
    total: int


# ==================== WebSocket 行情模型 ====================


class QuoteData(BaseModel):
    """实时行情数据（WebSocket 推送）。

    已知字段使用强类型，xtdata 返回的额外字段通过 ``model_extra`` 访问。
    """

    model_config = ConfigDict(extra="allow")

    stock_code: str | None = None
    timestamp: str | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    amount: float | None = None
    pre_close: float | None = None
    bid_price: list[float] | None = None
    ask_price: list[float] | None = None
    bid_vol: list[int] | None = None
    ask_vol: list[int] | None = None


# ==================== Tick / K线 / 除权 模型 ====================


class TickData(BaseModel):
    """Tick 快照数据。"""

    model_config = ConfigDict(extra="allow")

    time: str
    last_price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    last_close: float | None = None
    amount: float | None = None
    volume: int | None = None
    pvolume: int | None = None
    stock_status: int | None = None
    open_int: int | None = None
    last_settlement_price: float | None = None
    ask_price: list[float] | None = None
    bid_price: list[float] | None = None
    ask_vol: list[int] | None = None
    bid_vol: list[int] | None = None
    transaction_num: int | None = None


class FullTickResponse(BaseModel):
    """get_full_tick 返回：stock_code → tick 数据列表。"""

    ticks: Dict[str, List[TickData]]


class DividendFactor(BaseModel):
    """除权除息因子。"""

    time: str
    interest: float | None = None
    stock_bonus: float | None = None
    stock_gift: float | None = None
    allot_num: float | None = None
    allot_price: float | None = None
    gugai: int | None = None
    dr: float | None = None


# ==================== 下载 / 板块操作通用结果 ====================


class DownloadResult(BaseModel):
    """数据下载操作结果。"""

    model_config = ConfigDict(extra="allow")

    task_id: str | None = None
    status: str | None = None
    progress: float | None = None
    total: int | None = None
    finished: int | None = None
    message: str | None = None
    current_stock: str | None = None


class SectorOperationResult(BaseModel):
    """板块 CRUD 操作结果。"""

    model_config = ConfigDict(extra="allow")

    created_name: str | None = None
    success: bool | None = None
    message: str | None = None


# ==================== Level-2 数据模型 ====================


class L2QuoteData(BaseModel):
    """Level-2 快照行情（10 档盘口）。"""

    model_config = ConfigDict(extra="allow")

    time: str
    last_price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    amount: float | None = None
    volume: int | None = None
    pvolume: int | None = None
    open_int: int | None = None
    stock_status: int | None = None
    transaction_num: int | None = None
    last_close: float | None = None
    last_settlement_price: float | None = None
    settlement_price: float | None = None
    pe: float | None = None
    ask_price: list[float] | None = None
    bid_price: list[float] | None = None
    ask_vol: list[int] | None = None
    bid_vol: list[int] | None = None


class L2OrderData(BaseModel):
    """Level-2 逐笔委托。"""

    model_config = ConfigDict(extra="allow")

    time: str
    price: float
    volume: int
    entrust_no: int | None = None
    entrust_type: int | None = None
    entrust_direction: int | None = None


class L2TransactionData(BaseModel):
    """Level-2 逐笔成交。"""

    model_config = ConfigDict(extra="allow")

    time: str
    price: float
    volume: int
    amount: float | None = None
    trade_index: int | None = None
    buy_no: int | None = None
    sell_no: int | None = None
    trade_type: int | None = None
    trade_flag: int | None = None


class L2QuoteResponse(BaseModel):
    """get_l2_quote 返回：stock_code → L2QuoteData。"""

    quotes: Dict[str, L2QuoteData]


class L2OrderResponse(BaseModel):
    """get_l2_order 返回：stock_code → 委托列表。"""

    orders: Dict[str, List[L2OrderData]]


class L2TransactionResponse(BaseModel):
    """get_l2_transaction 返回：stock_code → 成交列表。"""

    transactions: Dict[str, List[L2TransactionData]]


__all__ = [
    "ConvertibleBondInfo",
    "DataDirResponse",
    "DividendFactor",
    "DownloadResult",
    "ETFInfoResponse",
    "FinancialDataResponse",
    "FullTickResponse",
    "HolidayInfo",
    "IndexWeightResponse",
    "InstrumentInfo",
    "InstrumentTypeInfo",
    "IpoInfo",
    "L2OrderData",
    "L2OrderResponse",
    "L2QuoteData",
    "L2QuoteResponse",
    "L2TransactionData",
    "L2TransactionResponse",
    "MarketDataResponse",
    "PeriodListResponse",
    "QuoteData",
    "SectorOperationResult",
    "SectorResponse",
    "SubscriptionCreateResult",
    "SubscriptionDeleteResult",
    "SubscriptionInfo",
    "SubscriptionListResult",
    "TickData",
    "TradingCalendarResponse",
]
