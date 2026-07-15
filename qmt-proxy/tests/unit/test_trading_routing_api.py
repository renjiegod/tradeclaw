"""REST 多终端路由测试：X-QMT-Terminal 头选择终端、未知终端 400、/clients 列表。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.dependencies as deps
from app.config import QmtClientConfig, Settings, XTQuantMode
from app.dependencies import CLIENT_ID_HEADER, get_settings, verify_api_key
from app.routers import trading


def _settings_two_mock_clients() -> Settings:
    settings = Settings()
    settings.xtquant.mode = XTQuantMode.MOCK
    settings.xtquant.clients = [
        QmtClientConfig(client_id="dgzq", name="券商A", qmt_userdata_path="C:\\dgzq", mode=XTQuantMode.MOCK),
        QmtClientConfig(client_id="gj", name="券商B", qmt_userdata_path="C:\\gj", mode=XTQuantMode.MOCK),
    ]
    settings.xtquant.default_client_id = "dgzq"
    return settings


@pytest.fixture()
def client():
    settings = _settings_two_mock_clients()
    deps._trading_manager_instance = None  # 重置单例，保证测试隔离
    fastapi_app = FastAPI()
    fastapi_app.include_router(trading.router)
    fastapi_app.dependency_overrides[get_settings] = lambda: settings
    fastapi_app.dependency_overrides[verify_api_key] = lambda: "test-key"
    with TestClient(fastapi_app) as test_client:
        yield test_client
    deps._trading_manager_instance = None


def test_list_clients_endpoint(client):
    resp = client.get("/api/v1/trading/clients")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["default_client_id"] == "dgzq"
    ids = {c["client_id"] for c in data["clients"]}
    assert ids == {"dgzq", "gj"}
    by_id = {c["client_id"]: c for c in data["clients"]}
    assert by_id["dgzq"]["is_default"] is True


def test_unknown_terminal_header_returns_400_with_error_code(client):
    resp = client.post(
        "/api/v1/trading/connect",
        json={"account_id": "acct-001"},
        headers={CLIENT_ID_HEADER: "ghost"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_code"] == "UNKNOWN_TERMINAL"
    assert "ghost" in detail["message"]


def test_connect_routes_to_selected_terminal(client):
    # 选 gj 终端（mock 模式）→ 连接成功
    resp = client.post(
        "/api/v1/trading/connect",
        json={"account_id": "acct-gj"},
        headers={CLIENT_ID_HEADER: "gj"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["session_id"]


def test_missing_header_falls_back_to_default_terminal(client):
    resp = client.post("/api/v1/trading/connect", json={"account_id": "acct-x"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
