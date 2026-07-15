"""
SystemApi 与根路径、应用信息测试（对应 libs/qmt_proxy_sdk/system.py）。

服务端对应关系：
- GET /health/、/health/ready、/health/live：app.routers.health，均经 format_response 返回信封；
  真实客户端由 AsyncHttpTransport 解包后得到 data，再校验为 HealthStatus / ServiceStatus。
- GET /、GET /info：app.main.root 与 app_info，同样为 format_response；data 字段与 RootInfo、AppInfo 模型对齐。

本文件 RecordingTransport 返回的是「与解包后一致的 dict」，用于隔离测试 Pydantic 解析与请求路径，
不启动 uvicorn。
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
    """按 (method, path) 返回预置响应，并记录调用；模拟已解包的业务 JSON（无信封外层）。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses[(method, path)]

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_client_exposes_system_api_with_typed_health_responses():
    """
    串联调用 check_health / check_ready / check_live，对应三条健康路由：

    - health_check：返回 status、app_name、app_version、xtquant_mode、timestamp（见 health.py）。
    - readiness_check：data.status == "ready"。
    - liveness_check：data.status == "alive"。

    断言 transport 调用顺序与路径与健康路由前缀 /health 一致。
    """
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    transport = RecordingTransport(
        {
            ("GET", "/health/"): {
                "status": "healthy",
                "app_name": "qmt-proxy",
                "app_version": "1.0.0",
                "xtquant_mode": "dev",
                "timestamp": "2026-03-21T12:00:00",
            },
            ("GET", "/health/ready"): {"status": "ready"},
            ("GET", "/health/live"): {"status": "alive"},
        }
    )
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    health = await client.system.check_health()
    ready = await client.system.check_ready()
    live = await client.system.check_live()

    assert health.status == "healthy"
    assert health.app_name == "qmt-proxy"
    assert ready.status == "ready"
    assert live.status == "alive"
    assert transport.calls == [
        ("GET", "/health/", {}),
        ("GET", "/health/ready", {}),
        ("GET", "/health/live", {}),
    ]
    logger.info(
        "system: health=%s ready=%s live=%s",
        health.status,
        ready.status,
        live.status,
    )


@pytest.mark.asyncio
async def test_system_api_returns_root_and_app_info_models():
    """
    get_root：对应 app.main.root，data 含 app_name、app_version、xtquant_mode、description、docs_url、redoc_url。

    get_info：对应 app.main.app_info，data 含 name、version、debug、host、port、log_level、
    xtquant_mode、allow_real_trading（来自 settings.xtquant.trading.allow_real_trading）。
    """
    client_module = _load_sdk_module("qmt_proxy_sdk.client")
    client_cls = getattr(client_module, "AsyncQmtProxyClient", None)
    assert client_cls is not None, "Expected AsyncQmtProxyClient to be exported"

    transport = RecordingTransport(
        {
            ("GET", "/"): {
                "app_name": "qmt-proxy",
                "app_version": "1.0.0",
                "xtquant_mode": "dev",
                "description": "基于xtquant的量化交易代理服务",
                "docs_url": "/docs",
                "redoc_url": "/redoc",
            },
            ("GET", "/info"): {
                "name": "qmt-proxy",
                "version": "1.0.0",
                "debug": False,
                "host": "0.0.0.0",
                "port": 8000,
                "log_level": "DEBUG",
                "xtquant_mode": "dev",
                "allow_real_trading": False,
            },
        }
    )
    client = client_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=transport,
    )

    root_info = await client.system.get_root()
    app_info = await client.system.get_info()

    assert root_info.docs_url == "/docs"
    assert root_info.redoc_url == "/redoc"
    assert app_info.name == "qmt-proxy"
    assert app_info.allow_real_trading is False
    logger.info("get_root docs_url=%s get_info allow_real_trading=%s", root_info.docs_url, app_info.allow_real_trading)
