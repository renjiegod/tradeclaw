"""API key 认证行为测试（走 app.main 真实异常 handler 链）。

覆盖四种路径：
- 无 Authorization 头   → 401 + WWW-Authenticate: Bearer + error_code=API_KEY_MISSING
- 错误 scheme（Basic）  → 401 + error_code=AUTHORIZATION_MALFORMED
- 无效 key             → 401 + error_code=API_KEY_INVALID（不回显白名单）
- 有效 key             → 200

历史 bug：AuthenticationException 落进 XTQuantException handler 被映射成 500。
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, XTQuantMode, get_settings
from app.main import app

VALID_KEY = "unit-test-key-001"

# 目标路由只依赖 verify_api_key + 内存诊断缓冲，无需真实 QMT / 订阅管理器。
PROTECTED_URL = "/api/v1/diagnostics/summary"


@pytest.fixture()
def client():
    settings = Settings()
    settings.xtquant.mode = XTQuantMode.MOCK
    settings.security.api_keys = [VALID_KEY]
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        # 不进 with（不触发 lifespan），避免初始化真实订阅管理器
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _assert_401_envelope(resp, expected_error_code: str):
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == 401
    assert body["error_code"] == expected_error_code
    assert body["message"]
    # 不得泄露白名单信息
    assert VALID_KEY not in resp.text


def test_missing_authorization_header_returns_401(client):
    resp = client.get(PROTECTED_URL)
    _assert_401_envelope(resp, "API_KEY_MISSING")


def test_wrong_auth_scheme_returns_401(client):
    resp = client.get(PROTECTED_URL, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    _assert_401_envelope(resp, "AUTHORIZATION_MALFORMED")


def test_invalid_api_key_returns_401(client):
    resp = client.get(PROTECTED_URL, headers={"Authorization": "Bearer not-in-whitelist"})
    _assert_401_envelope(resp, "API_KEY_INVALID")


def test_valid_api_key_returns_200(client):
    resp = client.get(PROTECTED_URL, headers={"Authorization": f"Bearer {VALID_KEY}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
