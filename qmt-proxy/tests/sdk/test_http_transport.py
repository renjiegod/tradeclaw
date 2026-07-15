"""
AsyncHttpTransport 行为测试（对应 libs/qmt_proxy_sdk/http.py）。

与服务端响应格式的关系：
- app.utils.helpers.format_response 产生统一信封：success、message、code、timestamp，可选 data。
- _looks_like_envelope 据此识别信封；成功时返回 data 字段供上层 Pydantic 校验。
- 部分路由直接返回 Pydantic 模型或 dict（无 success/message/code），transport 将整个 JSON 作为载荷返回。

错误映射与 app 侧一致参考：
- 401：无 Bearer 或密钥不在白名单时 verify_api_key 抛出 AuthenticationException，
  经 HTTP 异常处理转为 JSON（message 如「API密钥缺失」），transport 映射为 AuthenticationError。
- 422：请求体验证失败等，映射为 RequestValidationError（与 _map_error 一致）。
"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

import httpx
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


@pytest.mark.asyncio
async def test_transport_adds_bearer_auth_and_unwraps_enveloped_data():
    """
    初始化时若提供 api_key，应设置 Authorization: Bearer <token>（与 HTTPBearer 校验方式一致）。

    响应为 format_response 形状时，应解包出 data，供 SystemApi/DataApi 等与模型字段对齐。
    模拟路径 GET /health/ 对应健康检查，仅作路径示例。
    """
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            status_code=200,
            json={
                "success": True,
                "message": "ok",
                "code": 200,
                "timestamp": "2026-03-21T12:00:00",
                "data": {"status": "healthy"},
            },
        )

    http_module = _load_sdk_module("qmt_proxy_sdk.http")
    transport_cls = getattr(http_module, "AsyncHttpTransport", None)
    assert transport_cls is not None, "Expected AsyncHttpTransport to be exported"

    transport = transport_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=httpx.MockTransport(handler),
    )

    payload = await transport.request("GET", "/health/")

    assert captured == {
        "method": "GET",
        "path": "/health/",
        "auth": "Bearer your-api-key",
    }
    assert payload == {"status": "healthy"}
    logger.info("Mock 收到请求: %s，解包后 data=%s", captured, payload)

    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_keeps_raw_json_for_non_enveloped_payloads():
    """
    服务端直接返回业务 JSON（无 success/message/code）时，不解包。

    示例：POST /api/v1/trading/connect 使用 response_model=ConnectResponse，响应体即为连接结果字段，
    与 trading 路由 connect_account 返回一致（不经 format_response 再包一层时的形状）。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"session_id": "session-001", "message": "connected"},
        )

    http_module = _load_sdk_module("qmt_proxy_sdk.http")
    transport_cls = getattr(http_module, "AsyncHttpTransport", None)
    assert transport_cls is not None, "Expected AsyncHttpTransport to be exported"

    transport = transport_cls(
        base_url="http://localhost:8000",
        api_key="your-api-key",
        transport=httpx.MockTransport(handler),
    )

    payload = await transport.request("POST", "/api/v1/trading/connect", json={"account_id": "demo"})

    assert payload == {"session_id": "session-001", "message": "connected"}
    logger.info("非信封 JSON 原样返回: %s", payload)

    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_maps_http_errors_to_sdk_exceptions():
    """
    401 响应体含 message 时，应抛出 AuthenticationError，与无 API Key 时 verify_api_key 的错误文案一致。

    参考：app.dependencies.verify_api_key 在 api_key 为空时 raise AuthenticationException("API密钥缺失")。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            json={
                "success": False,
                "message": "API密钥缺失",
                "code": 401,
                "timestamp": "2026-03-21T12:00:00",
            },
        )

    http_module = _load_sdk_module("qmt_proxy_sdk.http")
    exceptions_module = _load_sdk_module("qmt_proxy_sdk.exceptions")
    transport_cls = getattr(http_module, "AsyncHttpTransport", None)
    auth_error_cls = getattr(exceptions_module, "AuthenticationError", None)

    assert transport_cls is not None, "Expected AsyncHttpTransport to be exported"
    assert auth_error_cls is not None, "Expected AuthenticationError to be exported"

    transport = transport_cls(
        base_url="http://localhost:8000",
        api_key="bad-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(auth_error_cls) as exc_info:
        await transport.request("GET", "/api/v1/data/sectors")

    assert "API密钥缺失" in str(exc_info.value)
    assert exc_info.value.code == 401
    logger.info("401 映射为 AuthenticationError: %s", exc_info.value)

    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_normalizes_error_code_to_int():
    """
    错误载荷中 code 为字符串时，_extract_code 应转为 int，便于 SDK 异常上 code 字段类型稳定。

    422 对应 FastAPI 校验失败；此处用 POST /api/v1/data/market 作为路径占位（真实服务需合法 body）。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=422,
            json={
                "success": False,
                "message": "bad payload",
                "code": "422",
                "timestamp": "2026-03-21T12:00:00",
            },
        )

    http_module = _load_sdk_module("qmt_proxy_sdk.http")
    exceptions_module = _load_sdk_module("qmt_proxy_sdk.exceptions")
    transport_cls = getattr(http_module, "AsyncHttpTransport", None)
    validation_error_cls = getattr(exceptions_module, "RequestValidationError", None)

    assert transport_cls is not None, "Expected AsyncHttpTransport to be exported"
    assert validation_error_cls is not None, "Expected RequestValidationError to be exported"

    transport = transport_cls(
        base_url="http://localhost:8000",
        api_key="bad-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(validation_error_cls) as exc_info:
        await transport.request("POST", "/api/v1/data/market", json={})

    assert exc_info.value.code == 422
    logger.info("字符串 code 已规范为 int: %s", exc_info.value.code)

    await transport.aclose()
