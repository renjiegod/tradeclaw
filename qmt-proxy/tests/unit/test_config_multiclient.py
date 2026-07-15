"""多客户端（多 QMT 终端）配置解析测试。"""
import os
import textwrap

import pytest

from app.config import (
    QmtClientConfig,
    Settings,
    XTQuantConfig,
    XTQuantDataConfig,
    XTQuantMode,
    XTQuantTradingConfig,
    load_config,
)


def _xtquant(**kwargs) -> XTQuantConfig:
    base = dict(
        mode=XTQuantMode.PROD,
        data=XTQuantDataConfig(qmt_userdata_path="C:\\legacy\\userdata_mini"),
        trading=XTQuantTradingConfig(allow_real_trading=True),
    )
    base.update(kwargs)
    return XTQuantConfig(**base)


def test_resolve_clients_backward_compat_synthesizes_single_default():
    cfg = _xtquant()  # 无 clients

    clients = cfg.resolve_clients()

    assert len(clients) == 1
    only = clients[0]
    assert only.client_id == "default"
    assert only.qmt_userdata_path == "C:\\legacy\\userdata_mini"
    assert only.mode == XTQuantMode.PROD
    assert only.allow_real_trading is True
    assert only.is_data_source is True
    # 默认/数据源解析在单终端下都指向 default
    assert cfg.resolve_default_client_id() == "default"
    assert cfg.resolve_data_source_client().client_id == "default"


def test_resolve_clients_fills_defaults_from_globals():
    cfg = _xtquant(
        clients=[
            # 留空 mode/allow_real_trading/path → 回退全局
            QmtClientConfig(client_id="a"),
            # 按终端覆盖
            QmtClientConfig(
                client_id="b",
                name="券商B模拟",
                qmt_userdata_path="C:\\gj\\userdata_mini",
                mode=XTQuantMode.DEV,
                allow_real_trading=False,
                is_data_source=True,
            ),
        ]
    )

    resolved = {c.client_id: c for c in cfg.resolve_clients()}

    # a 回退到全局值
    assert resolved["a"].qmt_userdata_path == "C:\\legacy\\userdata_mini"
    assert resolved["a"].mode == XTQuantMode.PROD
    assert resolved["a"].allow_real_trading is True
    assert resolved["a"].name == "a"  # name 留空时回退为 client_id
    # b 使用覆盖值
    assert resolved["b"].qmt_userdata_path == "C:\\gj\\userdata_mini"
    assert resolved["b"].mode == XTQuantMode.DEV
    assert resolved["b"].allow_real_trading is False
    assert resolved["b"].name == "券商B模拟"


def test_resolve_default_client_id_rules():
    cfg = _xtquant(clients=[QmtClientConfig(client_id="a"), QmtClientConfig(client_id="b")])
    assert cfg.resolve_default_client_id() == "a"  # 无显式配置 → 第一个

    cfg_explicit = _xtquant(
        clients=[QmtClientConfig(client_id="a"), QmtClientConfig(client_id="b")],
        default_client_id="b",
    )
    assert cfg_explicit.resolve_default_client_id() == "b"

    cfg_bad = _xtquant(
        clients=[QmtClientConfig(client_id="a")],
        default_client_id="ghost",  # 不存在 → 回退第一个
    )
    assert cfg_bad.resolve_default_client_id() == "a"


def test_resolve_client_id_validation():
    cfg = _xtquant(
        clients=[QmtClientConfig(client_id="a"), QmtClientConfig(client_id="b")],
        default_client_id="a",
    )
    assert cfg.resolve_client_id("b") == "b"      # 有效 → 原样
    assert cfg.resolve_client_id(None) == "a"     # 空 → 默认
    assert cfg.resolve_client_id("") == "a"       # 空串 → 默认
    assert cfg.resolve_client_id("ghost") is None  # 未知 → None（不静默回退）


def test_resolve_data_source_client_precedence():
    # 显式 data_source_client_id 优先
    cfg = _xtquant(
        clients=[
            QmtClientConfig(client_id="a", is_data_source=True),
            QmtClientConfig(client_id="b"),
        ],
        data_source_client_id="b",
    )
    assert cfg.resolve_data_source_client().client_id == "b"

    # 无显式配置 → is_data_source 标记
    cfg_flag = _xtquant(
        clients=[QmtClientConfig(client_id="a"), QmtClientConfig(client_id="b", is_data_source=True)],
    )
    assert cfg_flag.resolve_data_source_client().client_id == "b"

    # 都没有 → 默认 client
    cfg_none = _xtquant(
        clients=[QmtClientConfig(client_id="a"), QmtClientConfig(client_id="b")],
        default_client_id="b",
    )
    assert cfg_none.resolve_data_source_client().client_id == "b"


def test_get_client_present_and_absent():
    cfg = _xtquant(clients=[QmtClientConfig(client_id="a")])
    assert cfg.get_client("a") is not None
    assert cfg.get_client("missing") is None


def test_load_config_parses_clients_block(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yml"
    config_file.write_text(
        textwrap.dedent(
            """
            app:
              name: "qmt-proxy"
              version: "1.0.0"
            logging:
              format: "{time} | {level} | {message}"
            xtquant:
              qmt_userdata_path: "C:\\\\legacy\\\\userdata_mini"
              clients:
                - client_id: "dgzq_real"
                  name: "券商A-实盘"
                  qmt_userdata_path: "C:\\\\dgzq\\\\userdata_mini"
                  mode: "prod"
                  allow_real_trading: true
                  is_data_source: true
                - client_id: "gj_sim"
                  qmt_userdata_path: "C:\\\\gj\\\\userdata_mini"
                  mode: "dev"
              default_client_id: "dgzq_real"
              data_source_client_id: "dgzq_real"
            modes:
              prod:
                debug: false
                host: "0.0.0.0"
                port: 8000
                log_level: "INFO"
                xtquant_mode: "prod"
                allow_real_trading: true
                api_keys:
                  - "your-api-key"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_MODE", "prod")

    settings = load_config(str(config_file))

    clients = {c.client_id: c for c in settings.xtquant.resolve_clients()}
    assert set(clients) == {"dgzq_real", "gj_sim"}
    assert clients["dgzq_real"].qmt_userdata_path == "C:\\dgzq\\userdata_mini"
    assert clients["dgzq_real"].mode == XTQuantMode.PROD
    assert clients["dgzq_real"].allow_real_trading is True
    assert clients["gj_sim"].mode == XTQuantMode.DEV
    # gj_sim 未配置 allow_real_trading → 回退全局（prod 模式 allow_real_trading=true）
    assert clients["gj_sim"].allow_real_trading is True
    assert settings.xtquant.resolve_default_client_id() == "dgzq_real"
    assert settings.xtquant.resolve_data_source_client().client_id == "dgzq_real"


def test_settings_default_has_empty_clients_and_resolves_one():
    settings = Settings()
    assert settings.xtquant.clients == []
    assert len(settings.xtquant.resolve_clients()) == 1


def test_duplicate_client_id_raises():
    # 重复 client_id 会让后者静默覆盖前者（少一个终端），必须在启动期直接报错。
    cfg = _xtquant(
        clients=[
            QmtClientConfig(client_id="gj_sim", qmt_userdata_path="C:\\a"),
            QmtClientConfig(client_id="gj_sim", qmt_userdata_path="C:\\b"),
        ]
    )
    with pytest.raises(ValueError, match="重复的 client_id"):
        cfg.resolve_clients()
