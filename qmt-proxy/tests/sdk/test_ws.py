"""
WebSocket 行情流测试（对应 libs/qmt_proxy_sdk/ws.py）。

使用 mock 验证 QuoteStream 的订阅管理、消息解析、心跳和自动重连逻辑，
不依赖真实 WebSocket 服务器。
"""

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBS_ROOT = PROJECT_ROOT.parent  # canonical qmt_proxy_sdk now lives at monorepo root

if str(LIBS_ROOT) not in sys.path:
    sys.path.insert(0, str(LIBS_ROOT))


def _load(name: str):
    spec = importlib.util.find_spec(name)
    assert spec is not None, f"Module {name} not found"
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class FakeDataApi:
    """模拟 DataApi 的 create_subscription / delete_subscription。"""

    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_subscription(self, **kwargs):
        models = _load("qmt_proxy_sdk.models.data")
        self.created.append(kwargs)
        return models.SubscriptionCreateResult(
            subscription_id="sub-test-001",
            status="active",
        )

    async def delete_subscription(self, *, subscription_id: str):
        models = _load("qmt_proxy_sdk.models.data")
        self.deleted.append(subscription_id)
        return models.SubscriptionDeleteResult(
            success=True,
            message="ok",
            subscription_id=subscription_id,
        )


class FakeWebSocket:
    """模拟 websockets 连接，支持预设消息和发送记录。"""

    def __init__(self, messages: list[str]):
        self._messages = list(messages)
        self._idx = 0
        self.sent: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send(self, data: str):
        self.sent.append(data)

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_stream_yields_quote_data():
    """QuoteStream 应解析 WebSocket quote 消息为 QuoteData 模型。"""
    ws_module = _load("qmt_proxy_sdk.ws")
    models = _load("qmt_proxy_sdk.models.data")

    fake_api = FakeDataApi()
    messages = [
        json.dumps({"type": "connected", "subscription_id": "sub-test-001"}),
        json.dumps({
            "type": "quote",
            "data": {"stock_code": "000001.SZ", "last_price": 10.5, "volume": 1000},
            "timestamp": "2024-01-01T09:30:00",
        }),
        json.dumps({
            "type": "quote",
            "data": {"stock_code": "600000.SH", "last_price": 8.2, "volume": 500},
            "timestamp": "2024-01-01T09:30:01",
        }),
    ]
    fake_ws = FakeWebSocket(messages)

    stream = ws_module.QuoteStream(
        data_api=fake_api,
        ws_base_url="ws://localhost:8000",
        symbols=["000001.SZ", "600000.SH"],
    )

    collected: list = []
    with patch("qmt_proxy_sdk.ws.connect", return_value=fake_ws):
        async with stream:
            async for quote in stream:
                collected.append(quote)
                if len(collected) >= 2:
                    break

    assert len(collected) == 2
    assert isinstance(collected[0], models.QuoteData)
    assert collected[0].stock_code == "000001.SZ"
    assert collected[0].last_price == 10.5
    assert collected[1].stock_code == "600000.SH"

    assert len(fake_api.created) == 1
    assert fake_api.created[0]["symbols"] == ["000001.SZ", "600000.SH"]
    assert len(fake_api.deleted) == 1
    assert fake_api.deleted[0] == "sub-test-001"
    logger.info("QuoteStream 产出 %d 条行情", len(collected))


@pytest.mark.asyncio
async def test_quote_stream_normalizes_nested_xtdata_payload():
    """QuoteStream 应兼容服务端转发的 xtdata 嵌套行情结构。"""
    ws_module = _load("qmt_proxy_sdk.ws")

    fake_api = FakeDataApi()
    messages = [
        json.dumps({"type": "connected", "subscription_id": "sub-test-001"}),
        json.dumps({
            "type": "quote",
            "data": {
                "601166.SH": [{
                    "time": 1774579758000,
                    "lastPrice": 18.74,
                    "open": 18.8,
                    "high": 18.92,
                    "low": 18.7,
                    "lastClose": 18.88,
                    "amount": 531665380.0,
                    "volume": 282683,
                    "askPrice": [18.75, 18.76],
                    "bidPrice": [18.74, 18.73],
                    "askVol": [105, 231],
                    "bidVol": [5634, 821],
                    "transactionNum": 26065,
                }]
            },
            "timestamp": "2024-01-01T09:30:00",
        }),
    ]
    fake_ws = FakeWebSocket(messages)

    stream = ws_module.QuoteStream(
        data_api=fake_api,
        ws_base_url="ws://localhost:8000",
        symbols=["601166.SH"],
    )

    with patch("qmt_proxy_sdk.ws.connect", return_value=fake_ws):
        async for quote in stream:
            assert quote.stock_code == "601166.SH"
            assert quote.last_price == 18.74
            assert quote.pre_close == 18.88
            assert quote.ask_price == [18.75, 18.76]
            assert quote.bid_vol == [5634, 821]
            assert quote.timestamp == "2024-01-01T09:30:00"
            assert quote.model_extra["transactionNum"] == 26065
            break


@pytest.mark.asyncio
async def test_quote_stream_skips_pong_and_connected():
    """connected 和 pong 消息不应产出 QuoteData。"""
    ws_module = _load("qmt_proxy_sdk.ws")

    fake_api = FakeDataApi()
    messages = [
        json.dumps({"type": "connected", "subscription_id": "sub-test-001"}),
        json.dumps({"type": "pong", "timestamp": "2024-01-01T09:30:00"}),
        json.dumps({
            "type": "quote",
            "data": {"stock_code": "000001.SZ", "last_price": 10.0},
        }),
    ]
    fake_ws = FakeWebSocket(messages)

    stream = ws_module.QuoteStream(
        data_api=fake_api,
        ws_base_url="ws://localhost:8000",
        symbols=["000001.SZ"],
    )

    collected = []
    with patch("qmt_proxy_sdk.ws.connect", return_value=fake_ws):
        async for quote in stream:
            collected.append(quote)
            break

    assert len(collected) == 1
    assert collected[0].stock_code == "000001.SZ"


@pytest.mark.asyncio
async def test_quote_stream_raises_on_error_message():
    """收到 error 类型消息应抛出 QmtProxyError。"""
    ws_module = _load("qmt_proxy_sdk.ws")
    exc_module = _load("qmt_proxy_sdk.exceptions")

    fake_api = FakeDataApi()
    messages = [
        json.dumps({"type": "error", "message": "订阅不存在"}),
    ]
    fake_ws = FakeWebSocket(messages)

    stream = ws_module.QuoteStream(
        data_api=fake_api,
        ws_base_url="ws://localhost:8000",
        symbols=["000001.SZ"],
    )

    with pytest.raises(exc_module.QmtProxyError, match="订阅不存在"):
        with patch("qmt_proxy_sdk.ws.connect", return_value=fake_ws):
            async for _ in stream:
                pass


@pytest.mark.asyncio
async def test_quote_stream_context_manager_cleanup():
    """async with 退出应关闭流并清理订阅。"""
    ws_module = _load("qmt_proxy_sdk.ws")

    fake_api = FakeDataApi()
    messages = [
        json.dumps({"type": "quote", "data": {"stock_code": "000001.SZ", "last_price": 10.0}}),
    ]
    fake_ws = FakeWebSocket(messages)

    with patch("qmt_proxy_sdk.ws.connect", return_value=fake_ws):
        async with ws_module.QuoteStream(
            data_api=fake_api,
            ws_base_url="ws://localhost:8000",
            symbols=["000001.SZ"],
        ) as stream:
            assert stream.closed is False

        assert stream.closed is True


@pytest.mark.asyncio
async def test_subscribe_and_stream_method_exists():
    """client.data.subscribe_and_stream() 应返回 QuoteStream 实例。"""
    ws_module = _load("qmt_proxy_sdk.ws")
    client_module = _load("qmt_proxy_sdk.client")

    client = client_module.AsyncQmtProxyClient(
        base_url="http://localhost:8000",
        api_key="test-key",
    )
    stream = client.data.subscribe_and_stream(symbols=["000001.SZ"])
    assert isinstance(stream, ws_module.QuoteStream)
    assert stream.closed is False

    await client.aclose()


@pytest.mark.asyncio
async def test_quote_data_extra_fields():
    """QuoteData 应支持 extra='allow'，未知字段可通过 model_extra 访问。"""
    models = _load("qmt_proxy_sdk.models.data")

    quote = models.QuoteData.model_validate({
        "stock_code": "000001.SZ",
        "last_price": 10.5,
        "custom_field": "hello",
        "open_int": 42,
    })
    assert quote.stock_code == "000001.SZ"
    assert quote.model_extra["custom_field"] == "hello"
    assert quote.model_extra["open_int"] == 42
