"""DataService 数据源/路径解析（行情与券商无关，默认单数据源）测试。"""
import app.services.data_service as data_service_module
from app.config import QmtClientConfig, Settings, XTQuantMode
from app.services.data_service import DataService


def _settings(data_source_client_id=None):
    settings = Settings()
    settings.xtquant.mode = XTQuantMode.MOCK  # 避免初始化时真实连接
    settings.xtquant.data.qmt_userdata_path = "C:\\legacy\\userdata_mini"
    settings.xtquant.clients = [
        QmtClientConfig(client_id="a", qmt_userdata_path="C:\\a\\userdata_mini", mode=XTQuantMode.MOCK),
        QmtClientConfig(client_id="b", qmt_userdata_path="C:\\b\\userdata_mini", mode=XTQuantMode.MOCK),
    ]
    settings.xtquant.default_client_id = "a"
    if data_source_client_id:
        settings.xtquant.data_source_client_id = data_source_client_id
    return settings


def test_backward_compat_data_source_is_legacy_path():
    settings = Settings()
    settings.xtquant.mode = XTQuantMode.MOCK
    settings.xtquant.data.qmt_userdata_path = "C:\\legacy\\userdata_mini"

    svc = DataService(settings)

    assert svc._data_source_client_id == "default"
    assert svc._data_source_path == "C:\\legacy\\userdata_mini"
    assert svc._resolve_data_path(None) == "C:\\legacy\\userdata_mini"


def test_data_source_defaults_to_designated_client():
    svc = DataService(_settings(data_source_client_id="b"))

    assert svc._data_source_client_id == "b"
    assert svc._data_source_path == "C:\\b\\userdata_mini"
    # 缺省取数走数据源终端 b
    assert svc._resolve_data_path(None) == "C:\\b\\userdata_mini"
    assert svc._resolve_data_client_id(None) == "b"


def test_explicit_client_id_overrides_path_for_local_read():
    svc = DataService(_settings(data_source_client_id="b"))

    # 显式指定终端 a → 读 a 的本地 datadir
    assert svc._resolve_data_path("a") == "C:\\a\\userdata_mini"
    assert svc._resolve_data_client_id("a") == "a"


def test_unknown_client_id_falls_back_to_data_source():
    svc = DataService(_settings(data_source_client_id="b"))

    # 未知终端不静默乱指，回退数据源 b
    assert svc._resolve_data_path("ghost") == "C:\\b\\userdata_mini"
    assert svc._resolve_data_client_id("ghost") == "b"
