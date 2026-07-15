"""配置路由 /api/v1/config 的 GET/PUT + 鉴权测试（走 app.main 真实异常 handler 链）。

覆盖：
- 未鉴权（缺 Authorization）→ 401 + WWW-Authenticate: Bearer + error_code=API_KEY_MISSING
- 鉴权后 GET → 200，返回契约 B 的 data（path/app_mode/values/resolved_clients/
  restart_required_fields），api_keys 脱敏
- 鉴权后 PUT → 200，写回后 restart 信息正确，且能被随后 GET 读到
- PUT 坏值 → 400 + error_code=invalid_config + field
"""
import pytest
from fastapi.testclient import TestClient

import app.config_store as cs
from app.config import reset_settings
from app.main import app

VALID_KEY = "cfg-unit-test-key-001"
AUTH = {"Authorization": f"Bearer {VALID_KEY}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "qmt-proxy.yml"
    monkeypatch.setenv("QMT_PROXY_CONFIG", str(path))
    monkeypatch.setenv("APP_MODE", "dev")
    reset_settings()
    # 播种一份含白名单 api_key 的配置（dev 模式 → modes.dev.api_keys）
    cs.write_config({"security": {"api_keys": [VALID_KEY]}})
    reset_settings()
    try:
        # 不进 with（不触发 lifespan），避免初始化真实订阅管理器
        yield TestClient(app)
    finally:
        reset_settings()


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_get_config_unauthorized_returns_401(client):
    resp = client.get("/api/v1/config")
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "API_KEY_MISSING"


def test_put_config_unauthorized_returns_401(client):
    resp = client.put("/api/v1/config", json={"app": {"port": 8080}})
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    body = resp.json()
    assert body["error_code"] == "API_KEY_MISSING"


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #
def test_get_config_returns_masked_values(client):
    resp = client.get("/api/v1/config", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    # 契约 B data 结构
    assert set(data.keys()) >= {
        "path",
        "app_mode",
        "values",
        "resolved_clients",
        "restart_required_fields",
    }
    assert data["app_mode"] == "dev"
    # api_keys 脱敏，且真实 key 不回显
    assert data["values"]["security"]["api_keys"] == [cs.MASK]
    assert data["values"]["security"]["api_keys_set"] is True
    assert VALID_KEY not in resp.text


def test_put_config_updates_and_roundtrips(client):
    resp = client.put(
        "/api/v1/config",
        headers=AUTH,
        json={"xtquant": {"mode": "prod"}, "app": {"port": 8321}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "updated"
    assert data["restart_required"] is True
    assert set(data["restart_fields"]) == {"xtquant.mode", "app.port"}
    assert data["path"]

    # 随后 GET 能读到新值
    got = client.get("/api/v1/config", headers=AUTH).json()["data"]
    assert got["values"]["xtquant"]["mode"] == "prod"
    assert got["values"]["app"]["port"] == 8321


def test_put_config_validation_error_returns_400(client):
    resp = client.put(
        "/api/v1/config", headers=AUTH, json={"xtquant": {"mode": "bogus"}}
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error_code"] == "invalid_config"
    assert body["error_type"] == "validation_error"
    assert body["field"] == "xtquant.mode"


def test_put_config_duplicate_clients_returns_400(client):
    resp = client.put(
        "/api/v1/config",
        headers=AUTH,
        json={"xtquant": {"clients": [{"client_id": "x"}, {"client_id": "x"}]}},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error_code"] == "invalid_config"
    assert body["field"] == "xtquant.clients"
