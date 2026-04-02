from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel


class AccountType(str, Enum):
    FUTURE = "FUTURE"
    SECURITY = "SECURITY"
    CREDIT = "CREDIT"
    FUTURE_OPTION = "FUTURE_OPTION"
    STOCK_OPTION = "STOCK_OPTION"
    HUGANGTONG = "HUGANGTONG"
    INCOME_SWAP = "INCOME_SWAP"
    NEW3BOARD = "NEW3BOARD"
    SHENGANGTONG = "SHENGANGTONG"


class AccountInfo(BaseModel):
    account_id: str
    account_type: AccountType
    account_name: str
    status: str
    balance: float
    available_balance: float
    frozen_balance: float
    market_value: float
    total_asset: float


class PositionInfo(BaseModel):
    stock_code: str
    stock_name: str
    volume: int
    available_volume: int
    frozen_volume: int
    cost_price: float
    market_price: float
    market_value: float
    profit_loss: float
    profit_loss_ratio: float


class OrderResponse(BaseModel):
    order_id: str
    stock_code: str
    side: str
    order_type: str
    volume: int
    price: Optional[float]
    status: str
    submitted_time: datetime
    filled_volume: int = 0
    filled_amount: float = 0.0
    average_price: Optional[float] = None


class TradeInfo(BaseModel):
    trade_id: str
    order_id: str
    stock_code: str
    side: str
    volume: int
    price: float
    amount: float
    trade_time: datetime
    commission: float


class AssetInfo(BaseModel):
    total_asset: float
    market_value: float
    cash: float
    frozen_cash: float
    available_cash: float
    profit_loss: float
    profit_loss_ratio: float


class RiskInfo(BaseModel):
    position_ratio: float
    cash_ratio: float
    max_drawdown: float
    var_95: float
    var_99: float


class StrategyInfo(BaseModel):
    strategy_name: str
    strategy_type: str
    status: str
    created_time: datetime
    last_update_time: datetime
    parameters: Dict[str, Any]


class ConnectResponse(BaseModel):
    success: bool
    message: str
    session_id: Optional[str] = None
    account_info: Optional[AccountInfo] = None


class OperationResult(BaseModel):
    success: bool


class ConnectionStatus(BaseModel):
    connected: bool


__all__ = [
    "AccountType",
    "AccountInfo",
    "AssetInfo",
    "ConnectResponse",
    "ConnectionStatus",
    "OperationResult",
    "OrderResponse",
    "PositionInfo",
    "RiskInfo",
    "StrategyInfo",
    "TradeInfo",
]
