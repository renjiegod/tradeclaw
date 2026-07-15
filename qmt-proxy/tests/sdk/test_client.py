"""
AsyncQmtProxyClient 单元测试（对应 libs/qmt_proxy_sdk/client.py）。

服务端关联说明（仅便于理解客户端职责边界）：
- 真实环境下客户端经 AsyncHttpTransport 访问 app.main 挂载的路由（如 /health/、/api/v1/*），
  需携带 Bearer API Key（见 app.dependencies.verify_api_key）。
- 本文件不启动服务，用 DummyTransport 验证客户端如何管理 transport 与转发 request。
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
    """从仓库 libs/ 加载 SDK 包（与安装版 import 路径一致）。"""
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"Expected module '{module_name}' to exist under libs/"
    return importlib.import_module(module_name)


class DummyTransport:
    """最小异步 transport：记录 request 调用，用于验证客户端是否转发参数、是否误关外部资源。"""

    def __init__(self):
        self.closed = False
        self.calls = []

    async def aclose(self):
        self.closed = True

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"ok": True}


@pytest.mark.asyncio
async def test_client_closes_owned_transport():
    """
    未注入 transport 时，AsyncQmtProxyClient 内部创建 AsyncHttpTransport（内含 httpx.AsyncClient）。

    预期：aclose() 仅当 _owns_transport 为 True 时关闭底层 httpx 客户端，避免连接泄漏。
    对应实现：client.AsyncQmtProxyClient.aclose 中 if self._owns_transport。
    """
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    client = client_cls(base_url="http://localhost:8000", api_key="your-api-key")
    transport = getattr(client, "_transport", None)
    assert transport is not None, "Expected client to create an internal transport"

    await client.aclose()

    underlying_client = getattr(transport, "_client", None)
    assert underlying_client is not None, "Expected transport to own an httpx.AsyncClient"
    assert underlying_client.is_closed is True
    logger.info("自建 transport 已关闭，httpx 客户端 is_closed=%s", underlying_client.is_closed)


@pytest.mark.asyncio
async def test_client_does_not_close_injected_transport():
    """
    注入自定义 transport 时，客户端不应调用其 aclose()（由调用方管理生命周期）。

    典型场景：测试 Mock、连接池或与别组件共享的 AsyncHttpTransport。
    """
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    transport = DummyTransport()
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    await client.aclose()

    assert transport.closed is False
    logger.info("注入的 transport 未关闭: closed=%s", transport.closed)


@pytest.mark.asyncio
async def test_client_proxies_requests_to_transport():
    """
    request() 应原样委托给 _transport.request（方法、路径、关键字参数）。

    真实请求示例：GET /health/ 对应 app.routers.health.health_check（经 format_response 封装）；
    此处只验证转发，不校验响应信封。
    """
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    transport = DummyTransport()
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    result = await client.request("GET", "/health/")

    assert result == {"ok": True}
    assert transport.calls == [("GET", "/health/", {})]
    logger.info("request 转发: calls=%s result=%s", transport.calls, result)


@pytest.mark.asyncio
async def test_client_async_context_manager_closes_owned_transport():
    """async with 退出时应触发 aclose()，与 test_client_closes_owned_transport 行为一致。"""
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    async with client_cls(base_url="http://localhost:8000", api_key="your-api-key") as client:
        transport = getattr(client, "_transport", None)
        assert transport is not None, "Expected client to create an internal transport"

    underlying_client = getattr(transport, "_client", None)
    assert underlying_client is not None, "Expected transport to own an httpx.AsyncClient"
    assert underlying_client.is_closed is True
    logger.info("async with 退出后 httpx 已关闭: %s", underlying_client.is_closed)
