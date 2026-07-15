"""TradingClientManager 多终端路由测试。"""
import pytest

import app.services.trading_service as trading_service_module
from app.config import QmtClientConfig, Settings, XTQuantMode
from app.services.trading_manager import (
    UNKNOWN_TERMINAL_ERROR_CODE,
    TradingClientManager,
)
from app.utils.exceptions import TradingServiceException


@pytest.fixture(autouse=True)
def _no_real_xtquant(monkeypatch):
    # 强制走 mock 路径，避免在任何平台尝试真实连接 QMT。
    monkeypatch.setattr(trading_service_module, "XTQUANT_AVAILABLE", False)


def _multi_client_settings() -> Settings:
    settings = Settings()
    settings.xtquant.mode = XTQuantMode.PROD
    settings.xtquant.trading.allow_real_trading = True
    settings.xtquant.data.qmt_userdata_path = "C:\\legacy\\userdata_mini"
    settings.xtquant.clients = [
        QmtClientConfig(
            client_id="dgzq",
            name="券商A-实盘",
            qmt_userdata_path="C:\\dgzq\\userdata_mini",
            mode=XTQuantMode.PROD,
            allow_real_trading=True,
            is_data_source=True,
        ),
        QmtClientConfig(
            client_id="gj",
            name="券商B-模拟",
            qmt_userdata_path="C:\\gj\\userdata_mini",
            mode=XTQuantMode.DEV,
            allow_real_trading=False,
        ),
    ]
    settings.xtquant.default_client_id = "dgzq"
    return settings


def test_backward_compat_single_default_client():
    manager = TradingClientManager(Settings())

    assert manager.client_ids() == ["default"]
    assert manager.default_client_id == "default"
    # 缺省与显式 default 指向同一实例
    assert manager.get_service(None) is manager.get_service("default")


def test_multi_client_routes_to_distinct_services():
    manager = TradingClientManager(_multi_client_settings())

    svc_dgzq = manager.get_service("dgzq")
    svc_gj = manager.get_service("gj")

    # 不同终端 → 不同 TradingService 实例
    assert svc_dgzq is not svc_gj
    # 同终端再取 → 同实例（懒加载缓存）
    assert manager.get_service("dgzq") is svc_dgzq
    # 缺省路由到默认终端
    assert manager.get_service(None) is svc_dgzq


def test_per_client_settings_are_isolated():
    manager = TradingClientManager(_multi_client_settings())

    svc_dgzq = manager.get_service("dgzq")
    svc_gj = manager.get_service("gj")

    assert svc_dgzq.settings.xtquant.data.qmt_userdata_path == "C:\\dgzq\\userdata_mini"
    assert svc_dgzq.settings.xtquant.mode == XTQuantMode.PROD
    assert svc_dgzq.settings.xtquant.trading.allow_real_trading is True

    assert svc_gj.settings.xtquant.data.qmt_userdata_path == "C:\\gj\\userdata_mini"
    assert svc_gj.settings.xtquant.mode == XTQuantMode.DEV
    assert svc_gj.settings.xtquant.trading.allow_real_trading is False

    # 修改一个终端的 settings 不影响另一个 / 全局
    assert svc_dgzq.settings is not svc_gj.settings


def test_unknown_client_raises_with_error_code():
    manager = TradingClientManager(_multi_client_settings())

    with pytest.raises(TradingServiceException) as exc_info:
        manager.get_service("ghost")

    assert exc_info.value.error_code == UNKNOWN_TERMINAL_ERROR_CODE
    assert "ghost" in exc_info.value.message


def test_list_clients_reports_lazy_load_state():
    manager = TradingClientManager(_multi_client_settings())

    before = {c["client_id"]: c for c in manager.list_clients()}
    assert before["dgzq"]["loaded"] is False
    assert before["gj"]["loaded"] is False
    assert before["dgzq"]["is_default"] is True
    assert before["dgzq"]["is_data_source"] is True
    assert before["gj"]["is_default"] is False

    manager.get_service("dgzq")

    after = {c["client_id"]: c for c in manager.list_clients()}
    assert after["dgzq"]["loaded"] is True
    assert after["gj"]["loaded"] is False
    assert after["dgzq"]["mode"] == "prod"
    assert after["gj"]["allow_real_trading"] is False
