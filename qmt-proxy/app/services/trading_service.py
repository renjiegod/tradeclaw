"""
交易服务层
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List, Optional

from app.utils.logger import logger

# 添加xtquant包到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

try:
    import xtquant.xttrader as xttrader
    from xtquant import xtconstant
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount

    XTQUANT_AVAILABLE = True
except ImportError:
    logger.error("xtquant模块未正确安装")
    XTQUANT_AVAILABLE = False
    # 创建模拟模块以避免导入错误
    class MockModule:
        def __getattr__(self, name):
            def mock_function(*args, **kwargs):
                raise NotImplementedError(f"xtquant模块未正确安装，无法调用 {name}")
            return mock_function
    
    xttrader = MockModule()
    xtconstant = MockModule()
    XtQuantTrader = MockModule
    StockAccount = MockModule

from app.config import Settings, XTQuantMode
from app.models.trading_models import (
    AccountInfo,
    AccountType,
    AssetInfo,
    CancelOrderRequest,
    ConnectRequest,
    ConnectResponse,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionInfo,
    RiskInfo,
    StrategyInfo,
    TradeInfo,
)
from app.utils.exceptions import TradingServiceException
from app.utils.helpers import validate_stock_code
from app.utils.xttrader_diagnostics import format_xttrader_operation_failure


class TradingService:
    """交易服务类"""
    
    def __init__(self, settings: Settings, client_id: Optional[str] = None):
        """初始化交易服务

        ``client_id`` 标识本服务对接的 QMT 终端；所有实例日志都会带上
        ``[client_id]`` 标签，便于在多终端部署中区分日志来源。
        """
        self.settings = settings
        self._client_id = client_id or "default"
        # 绑定 client_id，使该终端的所有交易日志都带 [client_id] 标签。
        self._log = logger.bind(client_id=self._client_id)
        self._initialized = False
        self._connected_accounts = {}
        self._orders = {}
        self._trades = {}
        self._order_counter = 1000
        self._xt_trader = None
        self._init_failure_reason: Optional[str] = None
        self._try_initialize()
    
    def _try_initialize(self):
        """尝试初始化xttrader"""
        if not XTQUANT_AVAILABLE:
            self._initialized = False
            return
        
        if self.settings.xtquant.mode == XTQuantMode.MOCK:
            self._initialized = False
            return
        
        try:
            qmt_path = self.settings.xtquant.data.qmt_userdata_path
            if not qmt_path:
                self._init_failure_reason = (
                    "未配置 qmt_userdata_path，无法初始化 xttrader；"
                    "请在 config.yml 的 xtquant.qmt_userdata_path 中设置 QMT 的 userdata_mini 路径"
                )
                self._log.warning(self._init_failure_reason)
                self._initialized = False
                return

            trader_session = int(datetime.now().timestamp() * 1000) % 2147483647
            self._xt_trader = XtQuantTrader(qmt_path, trader_session)
            if hasattr(self._xt_trader, "start"):
                self._xt_trader.start()
            connect_result = self._xt_trader.connect()
            if connect_result != 0:
                failure_message = format_xttrader_operation_failure(
                    connect_result,
                    operation="连接",
                    qmt_userdata_path=qmt_path,
                    trader_session=trader_session,
                )
                self._init_failure_reason = failure_message
                self._log.warning(failure_message)
                self._xt_trader = None
                self._initialized = False
                return
            self._init_failure_reason = None
            self._initialized = True
            self._log.info("xttrader 已初始化")
        except Exception as e:
            self._init_failure_reason = f"xttrader 初始化异常: {e}"
            self._log.warning(self._init_failure_reason)
            self._xt_trader = None
            self._initialized = False
    
    def _should_use_real_trading(self) -> bool:
        """
        判断是否使用真实交易
        只有在 prod 模式且配置允许时才允许真实交易
        """
        return (
            self.settings.xtquant.mode == XTQuantMode.PROD and
            self.settings.xtquant.trading.allow_real_trading
        )
    
    def _should_use_real_data(self) -> bool:
        """
        判断是否连接xtquant获取真实数据（但不一定允许交易）
        dev 和 prod 模式都连接 xtquant
        """
        return (            
            self.settings.xtquant.mode in [XTQuantMode.DEV, XTQuantMode.PROD]
        )

    def _require_real_trading_backend(self):
        """确保真实交易后端可用于只读查询或真实交易。"""
        if not XTQUANT_AVAILABLE:
            raise TradingServiceException("xttrader 不可用，请确认 xtquant 已安装")
        if not self._initialized:
            if self._init_failure_reason:
                raise TradingServiceException(self._init_failure_reason)
            raise TradingServiceException("xttrader 未初始化或未连接")

    def _get_trader(self):
        """获取真实交易客户端。"""
        self._require_real_trading_backend()
        if self._xt_trader is None:
            raise TradingServiceException("xttrader 后端未就绪")
        return self._xt_trader

    def _new_session_id(self, account_id: str) -> str:
        return f"session_{account_id}_{datetime.now().timestamp()}"

    def _get_attr(self, obj: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            if isinstance(obj, dict) and name in obj:
                value = obj[name]
            else:
                value = getattr(obj, name, None)
            if value is not None:
                return value
        return default

    def _to_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _to_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _parse_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if value is None:
            return datetime.now()
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 1e12:
                numeric /= 1000.0
            try:
                return datetime.fromtimestamp(numeric)
            except (TypeError, ValueError, OSError, OverflowError):
                pass

        text = str(value).strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%H%M%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt == "%H%M%S":
                    return datetime.combine(datetime.now().date(), parsed.time())
                return parsed
            except ValueError:
                continue

        try:
            return datetime.fromtimestamp(float(value))
        except (TypeError, ValueError, OSError):
            return datetime.now()

    def _normalize_account_type(self, value: Any) -> AccountType:
        if isinstance(value, AccountType):
            return value
        if isinstance(value, int):
            account_type_by_code = {
                self._xt_constant_value("FUTURE_ACCOUNT", 1): AccountType.FUTURE,
                self._xt_constant_value("SECURITY_ACCOUNT", 2): AccountType.SECURITY,
                self._xt_constant_value("CREDIT_ACCOUNT", 3): AccountType.CREDIT,
                self._xt_constant_value("HUGANGTONG_ACCOUNT", 7): AccountType.HUGANGTONG,
                self._xt_constant_value("SHENGANGTONG_ACCOUNT", 11): AccountType.SHENGANGTONG,
            }
            mapped = account_type_by_code.get(value)
            if mapped is not None:
                return mapped
        if isinstance(value, str):
            normalized = value.upper()
            if normalized == "STOCK":
                return AccountType.SECURITY
            for account_type in AccountType:
                if account_type.value == normalized:
                    return account_type
        return AccountType.SECURITY

    def _map_account_status(self, raw_status: Any) -> str:
        if isinstance(raw_status, str):
            upper = raw_status.upper()
            if upper.isdigit():
                raw_status = int(upper)
            else:
                return upper

        status_by_code = {
            self._xt_constant_value("ACCOUNT_STATUS_OK", 0): "CONNECTED",
            self._xt_constant_value("ACCOUNT_STATUS_WAITING_LOGIN", 1): "CONNECTING",
            self._xt_constant_value("ACCOUNT_STATUSING", 2): "CONNECTING",
            self._xt_constant_value("ACCOUNT_STATUS_FAIL", 3): "FAILED",
            self._xt_constant_value("ACCOUNT_STATUS_INITING", 4): "INITIALIZING",
            self._xt_constant_value("ACCOUNT_STATUS_CORRECTING", 5): "SYNCING",
            self._xt_constant_value("ACCOUNT_STATUS_CLOSED", 6): "CLOSED",
        }
        if raw_status in status_by_code:
            return status_by_code[raw_status]
        if raw_status is None:
            return "CONNECTED"
        return str(raw_status)

    def _get_mock_account_info(self, account_id: str) -> AccountInfo:
        return AccountInfo(
            account_id=account_id,
            account_type=AccountType.SECURITY,
            account_name=f"账户{account_id}",
            status="CONNECTED",
            balance=1000000.0,
            available_balance=950000.0,
            frozen_balance=50000.0,
            market_value=800000.0,
            total_asset=1800000.0
        )

    def _store_mock_session(self, account_id: str, account_info: AccountInfo) -> str:
        session_id = self._new_session_id(account_id)
        self._connected_accounts[session_id] = {
            "account_id": account_id,
            "account_type": account_info.account_type.value,
            "account_info": account_info,
            "connected_time": datetime.now(),
        }
        return session_id

    def _get_session_context(self, session_id: str) -> dict:
        context = self._connected_accounts.get(session_id)
        if context is None:
            raise TradingServiceException("账户未连接")
        return context

    def _get_real_session_context(self, session_id: str) -> dict:
        context = self._get_session_context(session_id)
        if not self._should_use_real_data():
            return context

        self._require_real_trading_backend()
        if context.get("account") is None:
            raise TradingServiceException("真实账户上下文缺失，请重新连接账户")
        return context

    def _connect_real_account(self, request: ConnectRequest):
        account = StockAccount(request.account_id)
        subscribe_result = self._get_trader().subscribe(account)
        if subscribe_result != 0:
            raise TradingServiceException(
                format_xttrader_operation_failure(
                    subscribe_result,
                    operation="订阅交易账户",
                    qmt_userdata_path=self.settings.xtquant.data.qmt_userdata_path,
                    account_id=request.account_id,
                )
            )
        return account

    def _query_all_account_infos(self):
        trader = self._get_trader()
        if not hasattr(trader, "query_account_infos"):
            return []
        account_infos = trader.query_account_infos()
        return account_infos or []

    def _build_account_snapshot(self, account) -> SimpleNamespace:
        asset = self._get_trader().query_stock_asset(account)
        if asset is None:
            raise TradingServiceException("从QMT查询账户资产失败")

        raw_account_info = None
        for item in self._query_all_account_infos():
            if self._get_attr(item, "account_id") == getattr(account, "account_id", None):
                raw_account_info = item
                break

        available_balance = self._to_float(
            self._get_attr(asset, "available_cash", "cash", "enableBalance"),
            0.0,
        ) or 0.0
        frozen_balance = self._to_float(
            self._get_attr(asset, "frozen_cash", "frozen_balance"),
            0.0,
        ) or 0.0
        total_asset = self._to_float(
            self._get_attr(asset, "total_asset", "assetBalance", "current_balance"),
            0.0,
        ) or 0.0
        market_value = self._to_float(
            self._get_attr(asset, "market_value", "marketValue"),
            0.0,
        ) or 0.0
        balance = self._to_float(
            self._get_attr(asset, "current_balance", "balance"),
            available_balance + frozen_balance,
        ) or (available_balance + frozen_balance)

        return SimpleNamespace(
            account_id=self._get_attr(raw_account_info, "account_id", default=getattr(account, "account_id", "")),
            account_type=self._get_attr(raw_account_info, "account_type", default=getattr(account, "account_type", "STOCK")),
            account_name=self._get_attr(raw_account_info, "account_name", default=f"账户{getattr(account, 'account_id', '')}"),
            status=self._map_account_status(
                self._get_attr(raw_account_info, "login_status", "status", default="CONNECTED")
            ),
            balance=balance,
            available_balance=available_balance,
            frozen_balance=frozen_balance,
            market_value=market_value,
            total_asset=total_asset,
        )

    def _build_account_info_from_real_account(self, account) -> AccountInfo:
        return self._map_account_info(self._build_account_snapshot(account))

    def _store_real_session(self, request: ConnectRequest, account, account_info: AccountInfo) -> str:
        session_id = self._new_session_id(request.account_id)
        self._connected_accounts[session_id] = {
            "account_id": request.account_id,
            "account_type": account_info.account_type.value,
            "account": account,
            "account_info": account_info,
            "connected_time": datetime.now(),
        }
        return session_id

    def _query_real_account_info(self, session_id: str):
        context = self._get_real_session_context(session_id)
        return self._build_account_snapshot(context["account"])

    def _map_account_info(self, raw_account) -> AccountInfo:
        account_id = self._get_attr(raw_account, "account_id")
        total_asset = self._to_float(self._get_attr(raw_account, "total_asset"))
        if not account_id or total_asset is None:
            raise TradingServiceException("无法映射QMT账户信息")

        available_balance = self._to_float(self._get_attr(raw_account, "available_balance", "cash"), 0.0) or 0.0
        frozen_balance = self._to_float(self._get_attr(raw_account, "frozen_balance", "frozen_cash"), 0.0) or 0.0
        balance = self._to_float(self._get_attr(raw_account, "balance"), available_balance + frozen_balance)
        market_value = self._to_float(self._get_attr(raw_account, "market_value"), 0.0) or 0.0

        return AccountInfo(
            account_id=account_id,
            account_type=self._normalize_account_type(self._get_attr(raw_account, "account_type")),
            account_name=self._get_attr(raw_account, "account_name", default=f"账户{account_id}"),
            status=self._map_account_status(self._get_attr(raw_account, "status", default="CONNECTED")),
            balance=balance if balance is not None else available_balance + frozen_balance,
            available_balance=available_balance,
            frozen_balance=frozen_balance,
            market_value=market_value,
            total_asset=total_asset,
        )

    def _query_real_positions(self, session_id: str):
        context = self._get_real_session_context(session_id)
        positions = self._get_trader().query_stock_positions(context["account"])
        return positions or []

    def _map_position(self, raw_position) -> PositionInfo:
        stock_code = self._get_attr(raw_position, "stock_code", "stock_code1")
        volume = self._to_int(self._get_attr(raw_position, "volume", "total_volume"))
        if not stock_code or volume is None:
            raise TradingServiceException("无法映射QMT持仓信息")

        available_volume = self._to_int(
            self._get_attr(raw_position, "available_volume", "can_use_volume"),
            volume,
        )
        frozen_volume = self._to_int(
            self._get_attr(raw_position, "frozen_volume"),
            max(volume - (available_volume or 0), 0),
        )
        cost_price = self._to_float(
            self._get_attr(raw_position, "cost_price", "avg_price", "open_price"),
            0.0,
        ) or 0.0
        market_value = self._to_float(self._get_attr(raw_position, "market_value"), 0.0) or 0.0
        market_price = self._to_float(
            self._get_attr(raw_position, "market_price", "last_price"),
            None,
        )
        if market_price is None and volume:
            market_price = market_value / volume
        if market_price is None:
            market_price = 0.0

        profit_loss = self._to_float(
            self._get_attr(raw_position, "profit_loss", "float_profit", "position_profit"),
            None,
        )
        if profit_loss is None:
            profit_loss = market_value - cost_price * volume

        profit_loss_ratio = self._to_float(
            self._get_attr(raw_position, "profit_loss_ratio", "profit_rate"),
            None,
        )
        cost_basis = cost_price * volume
        if profit_loss_ratio is None:
            profit_loss_ratio = (profit_loss / cost_basis) if cost_basis else 0.0

        return PositionInfo(
            stock_code=stock_code,
            stock_name=self._get_attr(raw_position, "stock_name", "instrument_name", default=stock_code),
            volume=volume,
            available_volume=available_volume if available_volume is not None else volume,
            frozen_volume=frozen_volume if frozen_volume is not None else 0,
            cost_price=cost_price,
            market_price=market_price,
            market_value=market_value,
            profit_loss=profit_loss,
            profit_loss_ratio=profit_loss_ratio,
        )

    def _query_real_asset(self, session_id: str):
        context = self._get_real_session_context(session_id)
        asset = self._get_trader().query_stock_asset(context["account"])
        if asset is None:
            raise TradingServiceException("从QMT查询账户资产失败")
        return asset

    def _map_asset(self, raw_asset) -> AssetInfo:
        total_asset = self._to_float(self._get_attr(raw_asset, "total_asset", "assetBalance", "current_balance"))
        if total_asset is None:
            raise TradingServiceException("无法映射QMT资产信息")

        market_value = self._to_float(self._get_attr(raw_asset, "market_value", "marketValue"), 0.0) or 0.0
        cash = self._to_float(self._get_attr(raw_asset, "cash", "available_cash", "enableBalance"), 0.0) or 0.0
        frozen_cash = self._to_float(self._get_attr(raw_asset, "frozen_cash", "frozen_balance"), 0.0) or 0.0
        available_cash = self._to_float(self._get_attr(raw_asset, "available_cash", "cash", "enableBalance"), cash) or cash
        profit_loss = self._to_float(
            self._get_attr(raw_asset, "profit_loss", "float_profit"),
            total_asset - market_value - cash - frozen_cash,
        ) or 0.0
        profit_loss_ratio = self._to_float(self._get_attr(raw_asset, "profit_loss_ratio"), 0.0) or 0.0

        return AssetInfo(
            total_asset=total_asset,
            market_value=market_value,
            cash=cash,
            frozen_cash=frozen_cash,
            available_cash=available_cash,
            profit_loss=profit_loss,
            profit_loss_ratio=profit_loss_ratio,
        )

    def _query_real_orders(self, session_id: str):
        context = self._get_real_session_context(session_id)
        orders = self._get_trader().query_stock_orders(context["account"], False)
        return orders or []

    def _looks_like_proxy_order_type(self, value: Any) -> bool:
        return isinstance(value, str) and value.upper() in {member.value for member in OrderType}

    def _resolve_side_raw(self, obj: Any) -> Any:
        side = self._get_attr(obj, "side")
        if side is not None:
            return side
        offset_flag = self._get_attr(obj, "offset_flag")
        if offset_flag is not None:
            return offset_flag
        order_type = self._get_attr(obj, "order_type")
        if order_type is not None and not self._looks_like_proxy_order_type(order_type):
            return order_type
        return self._get_attr(obj, "direction")

    def _resolve_price_type_raw(self, obj: Any) -> Any:
        price_type = self._get_attr(obj, "price_type")
        if price_type is not None:
            return price_type
        order_type = self._get_attr(obj, "order_type")
        if order_type is not None and self._looks_like_proxy_order_type(order_type):
            return order_type
        return None

    def _xt_constant_value(self, name: str, fallback: Optional[int] = None) -> Optional[int]:
        value = getattr(xtconstant, name, fallback)
        if callable(value):
            return fallback
        return value

    def _collect_xt_constant_values(
        self,
        names: tuple[str, ...],
        fallbacks: Optional[dict[str, int]] = None,
    ) -> set[int]:
        fallbacks = fallbacks or {}
        values: set[int] = set()
        for name in names:
            value = self._xt_constant_value(name, fallbacks.get(name))
            if value is not None:
                values.add(value)
        return values

    def _side_buy_codes(self) -> set[int]:
        return self._collect_xt_constant_values(
            (
                "STOCK_BUY",
                "CREDIT_BUY",
                "CREDIT_FIN_BUY",
                "CREDIT_BUY_SECU_REPAY",
                "CREDIT_DIRECT_SECU_REPAY",
                "CREDIT_FIN_BUY_SPECIAL",
                "CREDIT_BUY_SECU_REPAY_SPECIAL",
                "CREDIT_DIRECT_SECU_REPAY_SPECIAL",
                "OFFSET_FLAG_OPEN",
            ),
            {
                "STOCK_BUY": 23,
                "CREDIT_BUY": 23,
                "CREDIT_FIN_BUY": 27,
                "CREDIT_BUY_SECU_REPAY": 29,
                "CREDIT_DIRECT_SECU_REPAY": 30,
                "CREDIT_FIN_BUY_SPECIAL": 40,
                "CREDIT_BUY_SECU_REPAY_SPECIAL": 42,
                "CREDIT_DIRECT_SECU_REPAY_SPECIAL": 43,
                "OFFSET_FLAG_OPEN": 48,
            },
        )

    def _side_sell_codes(self) -> set[int]:
        return self._collect_xt_constant_values(
            (
                "STOCK_SELL",
                "CREDIT_SELL",
                "CREDIT_SLO_SELL",
                "CREDIT_SELL_SECU_REPAY",
                "CREDIT_DIRECT_CASH_REPAY",
                "CREDIT_SLO_SELL_SPECIAL",
                "CREDIT_SELL_SECU_REPAY_SPECIAL",
                "CREDIT_DIRECT_CASH_REPAY_SPECIAL",
                "OFFSET_FLAG_CLOSE",
            ),
            {
                "STOCK_SELL": 24,
                "CREDIT_SELL": 24,
                "CREDIT_SLO_SELL": 28,
                "CREDIT_SELL_SECU_REPAY": 31,
                "CREDIT_DIRECT_CASH_REPAY": 32,
                "CREDIT_SLO_SELL_SPECIAL": 41,
                "CREDIT_SELL_SECU_REPAY_SPECIAL": 44,
                "CREDIT_DIRECT_CASH_REPAY_SPECIAL": 45,
                "OFFSET_FLAG_CLOSE": 49,
            },
        )

    def _limit_price_codes(self) -> set[int]:
        return self._collect_xt_constant_values(
            ("FIX_PRICE", "BROKER_PRICE_LIMIT"),
            {"BROKER_PRICE_LIMIT": 50},
        )

    def _market_price_codes(self) -> set[int]:
        return self._collect_xt_constant_values(
            (
                "LATEST_PRICE",
                "BROKER_PRICE_ANY",
                "MARKET_SH_CONVERT_5_CANCEL",
                "MARKET_SH_CONVERT_5_LIMIT",
                "MARKET_PEER_PRICE_FIRST",
                "MARKET_MINE_PRICE_FIRST",
                "MARKET_SZ_INSTBUSI_RESTCANCEL",
                "MARKET_SZ_CONVERT_5_CANCEL",
                "MARKET_SZ_FULL_OR_CANCEL",
            ),
            {
                "LATEST_PRICE": 5,
                "BROKER_PRICE_ANY": 49,
                "MARKET_SH_CONVERT_5_CANCEL": 42,
                "MARKET_SH_CONVERT_5_LIMIT": 43,
                "MARKET_PEER_PRICE_FIRST": 44,
                "MARKET_MINE_PRICE_FIRST": 45,
                "MARKET_SZ_INSTBUSI_RESTCANCEL": 46,
                "MARKET_SZ_CONVERT_5_CANCEL": 47,
                "MARKET_SZ_FULL_OR_CANCEL": 48,
            },
        )

    def _order_status_mapping(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        pending_codes = self._collect_xt_constant_values(
            ("ORDER_UNREPORTED", "ORDER_WAIT_REPORTING"),
            {"ORDER_UNREPORTED": 48, "ORDER_WAIT_REPORTING": 49},
        )
        submitted_codes = self._collect_xt_constant_values(
            ("ORDER_REPORTED", "ORDER_REPORTED_CANCEL"),
            {"ORDER_REPORTED": 50, "ORDER_REPORTED_CANCEL": 51},
        )
        partial_codes = self._collect_xt_constant_values(
            ("ORDER_PART_SUCC", "ORDER_PARTSUCC_CANCEL", "ORDER_PART_CANCEL"),
            {"ORDER_PART_SUCC": 55, "ORDER_PARTSUCC_CANCEL": 52, "ORDER_PART_CANCEL": 53},
        )
        for code in pending_codes:
            mapping[code] = OrderStatus.PENDING.value
        for code in submitted_codes:
            mapping[code] = OrderStatus.SUBMITTED.value
        for code in partial_codes:
            mapping[code] = OrderStatus.PARTIAL_FILLED.value
        for code in self._collect_xt_constant_values(
            ("ORDER_CANCELED",),
            {"ORDER_CANCELED": 54},
        ):
            mapping[code] = OrderStatus.CANCELLED.value
        for code in self._collect_xt_constant_values(
            ("ORDER_SUCCEEDED",),
            {"ORDER_SUCCEEDED": 56},
        ):
            mapping[code] = OrderStatus.FILLED.value
        for code in self._collect_xt_constant_values(
            ("ORDER_JUNK",),
            {"ORDER_JUNK": 57},
        ):
            mapping[code] = OrderStatus.REJECTED.value
        return mapping

    def _map_side(self, raw_value: Any) -> str:
        if isinstance(raw_value, str):
            upper = raw_value.upper()
            if upper in {OrderSide.BUY.value, OrderSide.SELL.value}:
                return upper
            if upper.isdigit():
                raw_value = int(upper)

        buy_codes = self._side_buy_codes()
        sell_codes = self._side_sell_codes()

        if raw_value in sell_codes:
            return OrderSide.SELL.value
        if raw_value in buy_codes:
            return OrderSide.BUY.value
        raise TradingServiceException(f"无法映射QMT买卖方向: {raw_value!r}")

    def _map_order_type_name(self, raw_value: Any) -> str:
        if isinstance(raw_value, str):
            upper = raw_value.upper()
            if upper in {member.value for member in OrderType}:
                return upper
        if raw_value is None:
            return OrderRequest.model_fields["order_type"].default.value

        limit_codes = self._limit_price_codes()
        market_codes = self._market_price_codes()

        if raw_value in limit_codes:
            return OrderType.LIMIT.value
        if raw_value in market_codes:
            return OrderType.MARKET.value
        if isinstance(raw_value, (int, float)):
            return str(int(raw_value))
        return OrderRequest.model_fields["order_type"].default.value

    def _map_order_status(self, raw_status: Any) -> str:
        if isinstance(raw_status, str):
            upper = raw_status.upper()
            if upper in {member.value for member in OrderStatus}:
                return upper
            if upper.isdigit():
                raw_status = int(upper)
            else:
                return upper
        if raw_status is None:
            return OrderStatus.SUBMITTED.value
        if isinstance(raw_status, (int, float)):
            status_code = int(raw_status)
            mapped = self._order_status_mapping().get(status_code)
            if mapped is not None:
                return mapped
            return str(status_code)
        return OrderStatus.SUBMITTED.value

    def _map_order(self, raw_order) -> OrderResponse:
        order_id = self._get_attr(raw_order, "order_id", "order_sysid")
        stock_code = self._get_attr(raw_order, "stock_code", "stock_code1")
        volume = self._to_int(self._get_attr(raw_order, "order_volume", "volume"))
        if order_id is None or not stock_code or volume is None:
            raise TradingServiceException("无法映射QMT委托信息")

        traded_volume = self._to_int(self._get_attr(raw_order, "traded_volume", "filled_volume"), 0) or 0
        traded_amount = self._to_float(self._get_attr(raw_order, "traded_amount", "filled_amount"), 0.0) or 0.0

        return OrderResponse(
            order_id=str(order_id),
            stock_code=stock_code,
            side=self._map_side(self._resolve_side_raw(raw_order)),
            order_type=self._map_order_type_name(self._resolve_price_type_raw(raw_order)),
            volume=volume,
            price=self._to_float(self._get_attr(raw_order, "price", "traded_price")),
            status=self._map_order_status(self._get_attr(raw_order, "order_status")),
            submitted_time=self._parse_datetime(self._get_attr(raw_order, "order_time")),
            filled_volume=traded_volume,
            filled_amount=traded_amount,
            average_price=self._to_float(self._get_attr(raw_order, "traded_price", "average_price")),
        )

    def _query_real_trades(self, session_id: str):
        context = self._get_real_session_context(session_id)
        trades = self._get_trader().query_stock_trades(context["account"])
        return trades or []

    def _map_trade(self, raw_trade) -> TradeInfo:
        trade_id = self._get_attr(raw_trade, "traded_id", "trade_id")
        stock_code = self._get_attr(raw_trade, "stock_code", "stock_code1")
        volume = self._to_int(self._get_attr(raw_trade, "traded_volume", "volume"))
        price = self._to_float(self._get_attr(raw_trade, "traded_price", "price"))
        if trade_id is None or not stock_code or volume is None or price is None:
            raise TradingServiceException("无法映射QMT成交信息")

        amount = self._to_float(self._get_attr(raw_trade, "traded_amount", "amount"), price * volume) or (price * volume)

        return TradeInfo(
            trade_id=str(trade_id),
            order_id=str(self._get_attr(raw_trade, "order_id", default="")),
            stock_code=stock_code,
            side=self._map_side(self._resolve_side_raw(raw_trade)),
            volume=volume,
            price=price,
            amount=amount,
            trade_time=self._parse_datetime(self._get_attr(raw_trade, "traded_time", "trade_time")),
            commission=self._to_float(self._get_attr(raw_trade, "commission"), 0.0) or 0.0,
        )

    def _map_xt_order_type(self, side: OrderSide) -> Any:
        if side == OrderSide.BUY:
            return self._xt_constant_value("STOCK_BUY", 23)
        return self._xt_constant_value("STOCK_SELL", 24)

    def _map_xt_price_type(self, order_type) -> Any:
        order_type_value = getattr(order_type, "value", str(order_type))
        if order_type_value == "LIMIT":
            return self._xt_constant_value("FIX_PRICE", 11)
        if order_type_value == "MARKET":
            return self._xt_constant_value("LATEST_PRICE", 5)
        raise TradingServiceException(f"暂不支持的QMT订单类型: {order_type_value}")
    
    def connect_account(self, request: ConnectRequest) -> ConnectResponse:
        """连接交易账户"""
        try:
            if not self._should_use_real_data():
                account_info = self._get_mock_account_info(request.account_id)
                session_id = self._store_mock_session(request.account_id, account_info)
                return ConnectResponse(
                    success=True,
                    message="账户连接成功",
                    session_id=session_id,
                    account_info=account_info
                )

            self._require_real_trading_backend()
            account = self._connect_real_account(request)
            account_info = self._build_account_info_from_real_account(account)
            session_id = self._store_real_session(request, account, account_info)

            return ConnectResponse(
                success=True,
                message="账户连接成功",
                session_id=session_id,
                account_info=account_info
            )
            
        except TradingServiceException as e:
            return ConnectResponse(
                success=False,
                message=str(e)
            )
        except Exception as e:
            return ConnectResponse(
                success=False,
                message=f"账户连接失败: {str(e)}"
            )
    
    def disconnect_account(self, session_id: str) -> bool:
        """断开交易账户"""
        try:
            if session_id in self._connected_accounts:
                del self._connected_accounts[session_id]
                return True
            return False
        except Exception as e:
            raise TradingServiceException(f"断开账户失败: {str(e)}")
    
    def get_account_info(self, session_id: str) -> AccountInfo:
        """获取账户信息"""
        context = self._get_session_context(session_id)
        if not self._should_use_real_data():
            return context["account_info"]

        try:
            return self._map_account_info(self._query_real_account_info(session_id))
        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"获取账户信息失败: {str(e)}")
    
    def get_positions(self, session_id: str) -> List[PositionInfo]:
        """获取持仓信息"""
        self._get_session_context(session_id)
        
        try:
            if not self._should_use_real_data():
                return [
                    PositionInfo(
                        stock_code="000001.SZ",
                        stock_name="平安银行",
                        volume=10000,
                        available_volume=10000,
                        frozen_volume=0,
                        cost_price=12.50,
                        market_price=13.20,
                        market_value=132000.0,
                        profit_loss=7000.0,
                        profit_loss_ratio=0.056
                    ),
                    PositionInfo(
                        stock_code="000002.SZ",
                        stock_name="万科A",
                        volume=5000,
                        available_volume=5000,
                        frozen_volume=0,
                        cost_price=18.80,
                        market_price=19.50,
                        market_value=97500.0,
                        profit_loss=3500.0,
                        profit_loss_ratio=0.037
                    )
                ]

            return [self._map_position(position) for position in self._query_real_positions(session_id)]

        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"获取持仓信息失败: {str(e)}")
    
    def submit_order(self, session_id: str, request: OrderRequest) -> OrderResponse:
        """提交订单"""
        self._get_session_context(session_id)
        
        try:
            if not validate_stock_code(request.stock_code):
                raise TradingServiceException(f"无效的股票代码: {request.stock_code}")
            
            # 🔒 关键拦截点：检查是否允许真实交易
            if not self._should_use_real_trading():
                self._log.warning(f"当前模式[{self.settings.xtquant.mode.value}]不允许真实交易，返回模拟订单")
                return self._get_mock_order_response(request)
            
            # ✅ 允许真实交易，调用xttrader提交订单
            self._log.info(f"真实交易模式：提交订单 {request.stock_code} {request.side.value} {request.volume}股")

            account = self._get_real_session_context(session_id)["account"]
            order_id = self._get_trader().order_stock(
                account,
                request.stock_code,
                self._map_xt_order_type(request.side),
                request.volume,
                self._map_xt_price_type(request.order_type),
                request.price or 0,
                request.strategy_name or "qmt-proxy",
                "submitted_by_proxy"
            )
            if order_id is None or (isinstance(order_id, int) and order_id <= 0):
                raise TradingServiceException("真实下单失败")
            
            order_response = OrderResponse(
                order_id=str(order_id),
                stock_code=request.stock_code,
                side=request.side.value,
                order_type=request.order_type.value,
                volume=request.volume,
                price=request.price,
                status=OrderStatus.SUBMITTED.value,
                submitted_time=datetime.now()
            )
            
            self._orders[str(order_id)] = order_response
            
            return order_response

        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"提交订单失败: {str(e)}")
    
    def _get_mock_order_response(self, request: OrderRequest) -> OrderResponse:
        """生成模拟订单响应"""
        order_id = f"mock_order_{self._order_counter}"
        self._order_counter += 1
        
        order_response = OrderResponse(
            order_id=order_id,
            stock_code=request.stock_code,
            side=request.side.value,
            order_type=request.order_type.value,
            volume=request.volume,
            price=request.price,
            status=OrderStatus.SUBMITTED.value,
            submitted_time=datetime.now()
        )
        
        self._orders[order_id] = order_response
        return order_response
    
    def cancel_order(self, session_id: str, request: CancelOrderRequest) -> bool:
        """撤销订单（dev/mock模式下总是拦截并返回True）"""
        self._get_session_context(session_id)
        
        # dev/mock模式下直接拦截，始终返回True
        if not self._should_use_real_trading():
            self._log.warning(f"当前模式[{self.settings.xtquant.mode.value}]不允许真实交易，撤单请求已拦截，直接返回True")
            # 如果有订单，标记为已撤销
            if request.order_id in self._orders:
                self._orders[request.order_id].status = OrderStatus.CANCELLED.value
            return True
        
        # prod模式下才做真实撤单校验
        try:
            self._log.info(f"真实交易模式：撤销订单 {request.order_id}")
            account = self._get_real_session_context(session_id)["account"]
            normalized_order_id = int(request.order_id) if str(request.order_id).isdigit() else request.order_id
            cancel_result = self._get_trader().cancel_order_stock(account, normalized_order_id)
            success = cancel_result in (0, True)
            if success and request.order_id in self._orders:
                self._orders[request.order_id].status = OrderStatus.CANCELLED.value
            return success
        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"撤销订单失败: {str(e)}")
    
    def get_orders(self, session_id: str) -> List[OrderResponse]:
        """获取订单列表"""
        self._get_session_context(session_id)
        
        try:
            if not self._should_use_real_data():
                return list(self._orders.values())

            return [self._map_order(order) for order in self._query_real_orders(session_id)]

        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"获取订单列表失败: {str(e)}")
    
    def get_trades(self, session_id: str) -> List[TradeInfo]:
        """获取成交记录"""
        self._get_session_context(session_id)
        
        try:
            if not self._should_use_real_data():
                return [
                    TradeInfo(
                        trade_id="trade_001",
                        order_id="order_1001",
                        stock_code="000001.SZ",
                        side="BUY",
                        volume=1000,
                        price=13.20,
                        amount=13200.0,
                        trade_time=datetime.now(),
                        commission=13.20
                    )
                ]

            return [self._map_trade(trade) for trade in self._query_real_trades(session_id)]

        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"获取成交记录失败: {str(e)}")
    
    def get_asset_info(self, session_id: str) -> AssetInfo:
        """获取资产信息"""
        self._get_session_context(session_id)
        
        try:
            if not self._should_use_real_data():
                return AssetInfo(
                    total_asset=1800000.0,
                    market_value=800000.0,
                    cash=950000.0,
                    frozen_cash=50000.0,
                    available_cash=900000.0,
                    profit_loss=50000.0,
                    profit_loss_ratio=0.028
                )

            return self._map_asset(self._query_real_asset(session_id))

        except TradingServiceException:
            raise
        except Exception as e:
            raise TradingServiceException(f"获取资产信息失败: {str(e)}")
    
    def get_risk_info(self, session_id: str) -> RiskInfo:
        """获取风险信息"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        try:
            # 这里可以添加风险计算逻辑
            return RiskInfo(
                position_ratio=0.44,  # 持仓比例
                cash_ratio=0.56,      # 现金比例
                max_drawdown=0.05,    # 最大回撤
                var_95=0.02,          # 95% VaR
                var_99=0.03           # 99% VaR
            )
            
        except Exception as e:
            raise TradingServiceException(f"获取风险信息失败: {str(e)}")
    
    def get_strategies(self, session_id: str) -> List[StrategyInfo]:
        """获取策略列表"""
        if session_id not in self._connected_accounts:
            raise TradingServiceException("账户未连接")
        
        try:
            # 模拟策略数据
            mock_strategies = [
                StrategyInfo(
                    strategy_name="MA策略",
                    strategy_type="TREND_FOLLOWING",
                    status="RUNNING",
                    created_time=datetime.now(),
                    last_update_time=datetime.now(),
                    parameters={"period": 20, "threshold": 0.02}
                ),
                StrategyInfo(
                    strategy_name="均值回归策略",
                    strategy_type="MEAN_REVERSION",
                    status="STOPPED",
                    created_time=datetime.now(),
                    last_update_time=datetime.now(),
                    parameters={"lookback": 10, "entry_threshold": 0.05}
                )
            ]
            
            return mock_strategies
            
        except Exception as e:
            raise TradingServiceException(f"获取策略列表失败: {str(e)}")
    
    def is_connected(self, session_id: str) -> bool:
        """检查账户是否连接"""
        return session_id in self._connected_accounts
