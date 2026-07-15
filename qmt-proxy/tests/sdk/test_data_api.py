"""
DataApi 测试（对应 libs/qmt_proxy_sdk/data.py 与 app/routers/data.py）。

说明：
- 路由前缀均为 /api/v1/data，且依赖 verify_api_key（Bearer Token）。
- RecordingTransport 返回的是「与 SDK transport 所见一致」的载荷（已解包的 data 或直接业务 JSON）。
"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBS_ROOT = PROJECT_ROOT.parent  # canonical qmt_proxy_sdk now lives at monorepo root

if str(LIBS_ROOT) not in sys.path:
    sys.path.insert(0, str(LIBS_ROOT))


def _load_sdk_module(module_name: str):
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"Expected module '{module_name}' to exist under libs/"
    return importlib.import_module(module_name)


class RecordingTransport:
    """按 (method, path) 返回预置 JSON，记录调用。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses[(method, path)]

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_client_exposes_data_api_with_typed_query_models():
    """串联调用多条 DataApi 只读接口，验证请求路径与 Pydantic 模型解析。"""
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/market"): [
                {
                    "stock_code": "000001.SZ",
                    "data": [{"close": 10.5}],
                    "fields": ["close"],
                    "period": "1d",
                    "start_date": "20240101",
                    "end_date": "20240131",
                }
            ],
            ("GET", "/api/v1/data/sectors"): [
                {
                    "sector_name": "银行",
                    "stock_list": ["000001.SZ"],
                    "sector_type": "industry",
                }
            ],
            ("POST", "/api/v1/data/sector"): {
                "sector_name": "银行",
                "stock_list": ["000001.SZ"],
                "sector_type": "industry",
            },
            ("POST", "/api/v1/data/index-weight"): {
                "index_code": "000300.SH",
                "date": "20240131",
                "weights": [{"stock_code": "000001.SZ", "weight": 0.1}],
            },
            ("GET", "/api/v1/data/trading-calendar/2024"): {
                "trading_dates": ["20240102"],
                "holidays": ["20240101"],
                "year": 2024,
            },
            ("GET", "/api/v1/data/instrument/000001.SZ"): {
                "ExchangeID": "SZ",
                "InstrumentID": "000001",
                "InstrumentName": "平安银行",
            },
            ("GET", "/api/v1/data/etf/510300.SH"): {
                "etf_code": "510300.SH",
                "etf_name": "沪深300ETF",
                "underlying_asset": "沪深300",
                "creation_unit": 1000000,
                "redemption_unit": 1000000,
            },
            ("GET", "/api/v1/data/holidays"): {"holidays": ["20240101"]},
            ("GET", "/api/v1/data/period-list"): {"periods": ["1d", "1m"]},
            ("GET", "/api/v1/data/data-dir"): {"data_dir": "C:/qmt/data"},
        }
    )
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    market = await client.data.get_market_data(
        stock_codes=["000001.SZ"],
        start_date="20240101",
        end_date="20240131",
    )
    sectors = await client.data.get_sector_list()
    sector = await client.data.get_stock_list_in_sector("银行")
    index_weight = await client.data.get_index_weight("000300.SH", date="20240131")
    calendar = await client.data.get_trading_calendar(2024)
    instrument = await client.data.get_instrument_info("000001.SZ")
    etf = await client.data.get_etf_info("510300.SH")
    holidays = await client.data.get_holidays()
    periods = await client.data.get_period_list()
    data_dir = await client.data.get_data_dir()

    assert market[0].stock_code == "000001.SZ"
    assert sectors[0].sector_name == "银行"
    assert sector.stock_list == ["000001.SZ"]
    assert index_weight.index_code == "000300.SH"
    assert calendar.year == 2024
    assert instrument.InstrumentID == "000001"
    assert etf.etf_code == "510300.SH"
    assert holidays.holidays == ["20240101"]
    assert periods.periods == ["1d", "1m"]
    assert data_dir.data_dir == "C:/qmt/data"
    logger.info(
        "data 串联: market=%d sectors=%d calls=%d",
        len(market),
        len(sectors),
        len(transport.calls),
    )


# ---------------------------------------------------------------------------
# 参数化测试：验证每个 DataApi 方法的 HTTP 路径 & 请求体
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "kwargs", "expected_method", "expected_path", "expected_kwargs", "response"),
    [
        # --- 财务与合约 ---
        (
            "get_financial_data",
            {"stock_codes": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131"},
            "POST",
            "/api/v1/data/financial",
            {"json": {"stock_codes": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131"}},
            [{"stock_code": "000001.SZ", "table_name": "balance", "data": [], "columns": []}],
        ),
        (
            "get_instrument_type",
            {"stock_code": "000001.SZ"},
            "GET",
            "/api/v1/data/instrument-type/000001.SZ",
            {},
            {"stock_code": "000001.SZ", "index": False, "stock": True, "fund": False, "etf": False, "bond": False, "option": False, "futures": False},
        ),
        (
            "get_convertible_bonds",
            {},
            "GET",
            "/api/v1/data/convertible-bonds",
            {},
            [{"bond_code": "110000"}],
        ),
        (
            "get_ipo_info",
            {},
            "GET",
            "/api/v1/data/ipo-info",
            {},
            [{"security_code": "301000"}],
        ),
        # --- 本地行情 / Tick / 除权 / K线 ---
        (
            "get_local_data",
            {"stock_codes": ["000001.SZ"], "start_time": "20240101", "end_time": "20240131"},
            "POST",
            "/api/v1/data/local-data",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "20240101", "end_time": "20240131", "period": "1d", "fields": None, "adjust_type": "none"}},
            [{"stock_code": "000001.SZ", "data": [], "fields": [], "period": "1d", "start_date": "", "end_date": ""}],
        ),
        (
            "get_full_tick",
            {"stock_codes": ["000001.SZ"]},
            "POST",
            "/api/v1/data/full-tick",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "", "end_time": ""}},
            {"000001.SZ": [{"time": "20240101120000", "last_price": 10.5}]},
        ),
        (
            "get_divid_factors",
            {"stock_code": "000001.SZ"},
            "POST",
            "/api/v1/data/divid-factors",
            {"json": {"stock_code": "000001.SZ"}},
            [{"time": "20240101", "interest": 0.5, "dr": 1.0}],
        ),
        (
            "get_full_kline",
            {"stock_codes": ["000001.SZ"]},
            "POST",
            "/api/v1/data/full-kline",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "", "end_time": "", "period": "1d", "fields": None, "adjust_type": "none"}},
            [{"stock_code": "000001.SZ", "data": [], "fields": [], "period": "1d", "start_date": "", "end_date": ""}],
        ),
        # --- 下载 ---
        (
            "download_history_data",
            {"stock_code": "000001.SZ", "period": "1d", "start_time": "20240101", "end_time": "20240131", "incrementally": True},
            "POST",
            "/api/v1/data/download/history-data",
            {"json": {"stock_code": "000001.SZ", "period": "1d", "start_time": "20240101", "end_time": "20240131", "incrementally": True}},
            {"task_id": "hist-1", "status": "completed", "progress": 100.0, "message": "ok"},
        ),
        (
            "download_history_data_batch",
            {"stock_list": ["000001.SZ"], "period": "1d", "start_time": "20240101", "end_time": "20240131"},
            "POST",
            "/api/v1/data/download/history-data-batch",
            {"json": {"stock_list": ["000001.SZ"], "period": "1d", "start_time": "20240101", "end_time": "20240131"}},
            {"task_id": "hist-batch-1", "status": "completed"},
        ),
        (
            "download_financial_data",
            {"stock_list": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131"},
            "POST",
            "/api/v1/data/download/financial-data",
            {"json": {"stock_list": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131"}},
            {"task_id": "fin-1", "status": "completed"},
        ),
        (
            "download_financial_data_batch",
            {"stock_list": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131", "callback_func": "cb"},
            "POST",
            "/api/v1/data/download/financial-data-batch",
            {"json": {"stock_list": ["000001.SZ"], "table_list": ["balance"], "start_date": "20240101", "end_date": "20240131", "callback_func": "cb"}},
            {"task_id": "fin-batch-1", "status": "completed"},
        ),
        ("download_sector_data", {}, "POST", "/api/v1/data/download/sector-data", {}, {"task_id": "sector-1", "status": "completed"}),
        (
            "download_index_weight",
            {"index_code": "000300.SH"},
            "POST",
            "/api/v1/data/download/index-weight",
            {"json": {"index_code": "000300.SH"}},
            {"task_id": "index-1", "status": "completed"},
        ),
        ("download_cb_data", {}, "POST", "/api/v1/data/download/cb-data", {}, {"task_id": "cb-1", "status": "completed"}),
        ("download_etf_info", {}, "POST", "/api/v1/data/download/etf-info", {}, {"task_id": "etf-1", "status": "completed"}),
        ("download_holiday_data", {}, "POST", "/api/v1/data/download/holiday-data", {}, {"task_id": "holiday-1", "status": "completed"}),
        (
            "download_history_contracts",
            {"market": "SH"},
            "POST",
            "/api/v1/data/download/history-contracts",
            {"json": {"market": "SH"}},
            {"task_id": "contract-1", "status": "completed"},
        ),
        # --- 板块管理 ---
        (
            "create_sector_folder",
            {"parent_node": "我的", "folder_name": "行业"},
            "POST",
            "/api/v1/data/sector/create-folder",
            {"params": {"parent_node": "我的", "folder_name": "行业"}},
            {"created_name": "行业", "success": True},
        ),
        (
            "create_sector",
            {"sector_name": "自选板块", "parent_node": "我的", "overwrite": False},
            "POST",
            "/api/v1/data/sector/create",
            {"json": {"parent_node": "我的", "sector_name": "自选板块", "overwrite": False}},
            {"created_name": "自选板块", "success": True},
        ),
        (
            "add_sector_stocks",
            {"sector_name": "自选板块", "stock_list": ["000001.SZ"]},
            "POST",
            "/api/v1/data/sector/add-stocks",
            {"json": {"sector_name": "自选板块", "stock_list": ["000001.SZ"]}},
            None,
        ),
        (
            "remove_sector_stocks",
            {"sector_name": "自选板块", "stock_list": ["000001.SZ"]},
            "POST",
            "/api/v1/data/sector/remove-stocks",
            {"json": {"sector_name": "自选板块", "stock_list": ["000001.SZ"]}},
            None,
        ),
        (
            "remove_sector",
            {"sector_name": "自选板块"},
            "POST",
            "/api/v1/data/sector/remove",
            {"params": {"sector_name": "自选板块"}},
            None,
        ),
        (
            "reset_sector",
            {"sector_name": "自选板块", "stock_list": ["000001.SZ"]},
            "POST",
            "/api/v1/data/sector/reset",
            {"json": {"sector_name": "自选板块", "stock_list": ["000001.SZ"]}},
            None,
        ),
        # --- Level-2 ---
        (
            "get_l2_quote",
            {"stock_codes": ["000001.SZ"]},
            "POST",
            "/api/v1/data/l2/quote",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "", "end_time": ""}},
            {"000001.SZ": {"time": "20240101", "last_price": 10.5}},
        ),
        (
            "get_l2_order",
            {"stock_codes": ["000001.SZ"]},
            "POST",
            "/api/v1/data/l2/order",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "", "end_time": ""}},
            {"000001.SZ": [{"time": "20240101", "price": 10.5, "volume": 100}]},
        ),
        (
            "get_l2_transaction",
            {"stock_codes": ["000001.SZ"]},
            "POST",
            "/api/v1/data/l2/transaction",
            {"json": {"stock_codes": ["000001.SZ"], "start_time": "", "end_time": ""}},
            {"000001.SZ": [{"time": "20240101", "price": 10.5, "volume": 100}]},
        ),
        # --- 订阅 ---
        (
            "create_subscription",
            {"symbols": ["000001.SZ"], "period": "tick", "start_date": "20240101", "adjust_type": "none", "subscription_type": "quote"},
            "POST",
            "/api/v1/data/subscription",
            {"json": {"symbols": ["000001.SZ"], "period": "tick", "start_date": "20240101", "adjust_type": "none", "subscription_type": "quote"}},
            {"subscription_id": "sub-1", "status": "active"},
        ),
        (
            "delete_subscription",
            {"subscription_id": "sub-1"},
            "DELETE",
            "/api/v1/data/subscription/sub-1",
            {},
            {"success": True, "message": "订阅已取消", "subscription_id": "sub-1"},
        ),
        (
            "get_subscription",
            {"subscription_id": "sub-1"},
            "GET",
            "/api/v1/data/subscription/sub-1",
            {},
            {"subscription_id": "sub-1", "active": True},
        ),
        (
            "list_subscriptions",
            {},
            "GET",
            "/api/v1/data/subscriptions",
            {},
            {"subscriptions": [], "total": 0},
        ),
    ],
)
async def test_data_api_routes(
    method_name,
    kwargs,
    expected_method,
    expected_path,
    expected_kwargs,
    response,
):
    """逐条校验 DataApi 方法名 → HTTP 方法、路径、请求体。"""
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None

    transport = RecordingTransport({(expected_method, expected_path): response})
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    method = getattr(client.data, method_name)
    result = await method(**kwargs)

    assert result is not None
    assert transport.calls == [(expected_method, expected_path, expected_kwargs)]
    logger.info("data.%s -> %s %s OK", method_name, expected_method, expected_path)


# ---------------------------------------------------------------------------
# 返回类型断言
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_returns_download_result():
    """download_* 方法应返回 DownloadResult 模型。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/download/history-data"): {
                "task_id": "t1",
                "status": "completed",
                "progress": 100.0,
                "message": "done",
            },
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.download_history_data(stock_code="000001.SZ")
    assert isinstance(result, models.DownloadResult)
    assert result.task_id == "t1"
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_full_tick_returns_typed_response():
    """get_full_tick 应返回 FullTickResponse，内含 TickData 列表。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/full-tick"): {
                "000001.SZ": [
                    {"time": "20240101120000", "last_price": 10.5, "volume": 1000}
                ]
            },
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.get_full_tick(stock_codes=["000001.SZ"])
    assert isinstance(result, models.FullTickResponse)
    assert "000001.SZ" in result.ticks
    assert result.ticks["000001.SZ"][0].last_price == 10.5


@pytest.mark.asyncio
async def test_l2_quote_returns_typed_response():
    """get_l2_quote 应返回 L2QuoteResponse，按 stock_code 索引。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/l2/quote"): {
                "000001.SZ": {
                    "time": "20240101",
                    "last_price": 10.5,
                    "ask_price": [10.6, 10.7],
                    "bid_price": [10.4, 10.3],
                }
            },
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.get_l2_quote(stock_codes=["000001.SZ"])
    assert isinstance(result, models.L2QuoteResponse)
    assert "000001.SZ" in result.quotes
    assert result.quotes["000001.SZ"].last_price == 10.5


@pytest.mark.asyncio
async def test_divid_factors_returns_list():
    """get_divid_factors 应返回 DividendFactor 列表。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/divid-factors"): [
                {"time": "20240101", "interest": 0.5, "dr": 1.0}
            ],
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.get_divid_factors(stock_code="000001.SZ")
    assert isinstance(result, list)
    assert isinstance(result[0], models.DividendFactor)
    assert result[0].interest == 0.5


@pytest.mark.asyncio
async def test_sector_crud_returns_operation_result():
    """板块 CRUD 应返回 SectorOperationResult。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("POST", "/api/v1/data/sector/create"): {
                "created_name": "自选",
                "success": True,
                "message": "ok",
            },
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.create_sector(sector_name="自选")
    assert isinstance(result, models.SectorOperationResult)
    assert result.created_name == "自选"
    assert result.success is True


@pytest.mark.asyncio
async def test_subscription_list_uses_typed_subscriptions():
    """SubscriptionListResult.subscriptions 应为 list[SubscriptionInfo]。"""
    models = _load_sdk_module("qmt_proxy_sdk.models.data")
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = client_module.AsyncQmtProxyClient

    transport = RecordingTransport(
        {
            ("GET", "/api/v1/data/subscriptions"): {
                "subscriptions": [
                    {"subscription_id": "sub-1", "active": True, "symbols": ["000001.SZ"]},
                ],
                "total": 1,
            },
        }
    )
    client = client_cls(base_url="http://localhost:8000", transport=transport)
    result = await client.data.list_subscriptions()
    assert isinstance(result, models.SubscriptionListResult)
    assert len(result.subscriptions) == 1
    assert isinstance(result.subscriptions[0], models.SubscriptionInfo)
    assert result.subscriptions[0].subscription_id == "sub-1"
