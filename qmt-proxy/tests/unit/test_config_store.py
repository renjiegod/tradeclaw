"""config_store 单元测试：mode-aware 写回 round-trip + 脱敏 + seeding + clients 去重。

契约见共享规格「契约 B」。重点验证：
- mode 相关字段落到 ``modes.<当前 APP_MODE>``，模式无关字段落顶层（写错位置会丢配置）；
- secret（api_keys/secret_key）GET 脱敏、PUT 收到掩码时保留原值；
- 缺文件时 seeding 出 ``modes.{mock,dev,prod}``，注释保留；
- clients 重复 id / 缺 client_id / 非法 mode / 非法 port → 结构化 ConfigStoreError。
"""
import os

import pytest
import yaml

import app.config_store as cs
from app.config import reset_settings, resolve_config_path


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    """把配置指向 tmp 文件，APP_MODE 缺省 dev；每个用例前后 reset 单例。"""
    path = tmp_path / "qmt-proxy.yml"
    monkeypatch.setenv("QMT_PROXY_CONFIG", str(path))
    monkeypatch.setenv("APP_MODE", "dev")
    reset_settings()
    try:
        yield path
    finally:
        reset_settings()


def _raw(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
def test_seed_on_missing_file_creates_full_structure(cfg_path):
    assert not cfg_path.exists()
    cs.write_config({})  # 空 patch 也会触发 seeding + 写盘
    assert cfg_path.exists()

    data = _raw(cfg_path)
    assert set(data["modes"].keys()) == {"mock", "dev", "prod"}
    # 顶层与模式无关的段存在
    assert "clients" in data["xtquant"]
    assert "grpc" in data
    assert "security" in data
    # 每个模式段有 mode-aware 字段占位
    for mode in ("mock", "dev", "prod"):
        assert "xtquant_mode" in data["modes"][mode]
        assert "api_keys" in data["modes"][mode]


def test_seed_preserves_comments(cfg_path):
    cs.write_config({"logging": {"level": "DEBUG"}})
    text = cfg_path.read_text(encoding="utf-8")
    assert "# qmt-proxy 服务端配置" in text  # 注释被 ruamel round-trip 保留
    assert "modes:" in text


def test_seed_config_if_missing_helper(cfg_path):
    resolved = cs.seed_config_if_missing()
    assert resolved == str(cfg_path) == resolve_config_path()
    assert cfg_path.exists()
    # 已存在时不覆盖（不报错，原样返回）
    cfg_path.write_text("app:\n  name: sentinel\n", encoding="utf-8")
    again = cs.seed_config_if_missing()
    assert again == str(cfg_path)
    assert _raw(cfg_path)["app"]["name"] == "sentinel"


# --------------------------------------------------------------------------- #
# mode-aware writeback
# --------------------------------------------------------------------------- #
def test_mode_aware_writeback_targets_current_mode(cfg_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "prod")
    reset_settings()

    cs.write_config(
        {
            "xtquant": {
                "mode": "dev",  # → modes.prod.xtquant_mode
                "trading": {"allow_real_trading": True},  # → modes.prod.allow_real_trading
                "data": {"qmt_userdata_path": "C:/qmt/userdata_mini"},  # → 顶层
                "clients": [{"client_id": "gj", "qmt_userdata_path": "C:/gj"}],  # → 顶层
                "default_client_id": "gj",  # → 顶层
                "data_source_client_id": "gj",  # → 顶层
            },
            "security": {"api_keys": ["key-a"]},  # → modes.prod.api_keys
            "app": {"host": "127.0.0.1", "port": 9001},  # → modes.prod.host/port
            "logging": {"level": "WARNING"},  # → modes.prod.log_level
            "grpc": {"enabled": False, "host": "0.0.0.0", "port": 50055},  # → 顶层
        }
    )

    data = _raw(cfg_path)
    prod = data["modes"]["prod"]
    # mode-aware → modes.prod
    assert prod["xtquant_mode"] == "dev"
    assert prod["allow_real_trading"] is True
    assert prod["api_keys"] == ["key-a"]
    assert prod["host"] == "127.0.0.1"
    assert prod["port"] == 9001
    assert prod["log_level"] == "WARNING"
    # 不能污染其它模式段
    assert data["modes"]["dev"]["xtquant_mode"] == "dev"  # seed 默认值未被改
    assert data["modes"]["dev"]["port"] == 8000

    # 模式无关 → 顶层
    assert data["xtquant"]["qmt_userdata_path"] == "C:/qmt/userdata_mini"
    assert data["xtquant"]["clients"] == [{"client_id": "gj", "qmt_userdata_path": "C:/gj"}]
    assert data["xtquant"]["default_client_id"] == "gj"
    assert data["xtquant"]["data_source_client_id"] == "gj"
    assert data["grpc"]["enabled"] is False
    assert data["grpc"]["host"] == "0.0.0.0"
    assert data["grpc"]["port"] == 50055


def test_get_reflects_written_values_round_trip(cfg_path):
    cs.write_config(
        {
            "xtquant": {"mode": "prod", "trading": {"allow_real_trading": True}},
            "app": {"port": 8123},
            "grpc": {"enabled": False},
        }
    )
    g = cs.read_config_masked()
    assert g["app_mode"] == "dev"
    assert g["path"] == str(cfg_path)
    assert g["values"]["xtquant"]["mode"] == "prod"
    assert g["values"]["xtquant"]["trading"]["allow_real_trading"] is True
    assert g["values"]["app"]["port"] == 8123
    assert g["values"]["grpc"]["enabled"] is False
    assert g["restart_required_fields"] == cs.RESTART_REQUIRED_FIELDS


def test_qmt_userdata_path_empty_string_coerced_to_null(cfg_path):
    # 先设真实路径，再用空串清空 → 归一为 null（未设），而不是写入 ""
    # （"" 会让 xtdata.data_dir 指向空串，语义不同于未设的回退）。
    cs.write_config({"xtquant": {"data": {"qmt_userdata_path": "C:/qmt/userdata_mini"}}})
    res = cs.write_config({"xtquant": {"data": {"qmt_userdata_path": ""}}})
    # 用户清空是一次真实改动 → 计入 restart_fields
    assert "xtquant.data.qmt_userdata_path" in res["restart_fields"]
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["xtquant"]["qmt_userdata_path"] is None
    g = cs.read_config_masked()
    assert g["values"]["xtquant"]["data"]["qmt_userdata_path"] is None


def test_get_returns_resolved_clients(cfg_path):
    cs.write_config(
        {"xtquant": {"clients": [{"client_id": "a"}, {"client_id": "b", "mode": "dev"}]}}
    )
    g = cs.read_config_masked()
    ids = [c["client_id"] for c in g["resolved_clients"]]
    assert ids == ["a", "b"]
    # values.clients 是原始配置（未解析默认值）
    assert [c["client_id"] for c in g["values"]["xtquant"]["clients"]] == ["a", "b"]


# --------------------------------------------------------------------------- #
# secret masking / preservation
# --------------------------------------------------------------------------- #
def test_api_keys_masked_in_get(cfg_path):
    cs.write_config({"security": {"api_keys": ["real-1", "real-2"]}})
    g = cs.read_config_masked()
    sec = g["values"]["security"]
    assert sec["api_keys"] == [cs.MASK, cs.MASK]  # 脱敏，不回显真实 key
    assert sec["api_keys_set"] is True
    assert sec["api_keys_count"] == 2
    # 原始 key 不出现在返回里
    import json

    assert "real-1" not in json.dumps(g)


def test_masked_api_keys_put_preserves_original(cfg_path):
    cs.write_config({"security": {"api_keys": ["real-1", "real-2"]}})
    # 用户没改 → 前端把脱敏值原样回传
    res = cs.write_config({"security": {"api_keys": [cs.MASK, cs.MASK]}})
    assert "security.api_keys" not in res["restart_fields"]  # 未改 → 不计重启
    data = _raw(cfg_path)
    assert data["modes"]["dev"]["api_keys"] == ["real-1", "real-2"]  # 原值保留


def test_empty_api_keys_list_preserves_original(cfg_path):
    cs.write_config({"security": {"api_keys": ["real-1"]}})
    cs.write_config({"security": {"api_keys": []}})  # 空列表视为脱敏占位 → 保留
    assert _raw(cfg_path)["modes"]["dev"]["api_keys"] == ["real-1"]


def test_real_api_keys_replace(cfg_path):
    cs.write_config({"security": {"api_keys": ["old"]}})
    res = cs.write_config({"security": {"api_keys": ["new-1", "new-2"]}})
    assert "security.api_keys" in res["restart_fields"]
    assert _raw(cfg_path)["modes"]["dev"]["api_keys"] == ["new-1", "new-2"]


def test_secret_key_masked_write_preserved(cfg_path):
    cs.write_config({"security": {"secret_key": "s3cret"}})
    cs.write_config({"security": {"secret_key": cs.MASK}})  # 掩码 → 保留
    assert _raw(cfg_path)["security"]["secret_key"] == "s3cret"


def test_secret_key_not_restart_field(cfg_path):
    res = cs.write_config({"security": {"secret_key": "brand-new"}})
    # secret_key 不在契约 restart_required_fields 里
    assert res["restart_fields"] == []
    assert res["restart_required"] is False
    assert _raw(cfg_path)["security"]["secret_key"] == "brand-new"


# --------------------------------------------------------------------------- #
# restart fields
# --------------------------------------------------------------------------- #
def test_restart_fields_only_include_patched(cfg_path):
    res = cs.write_config({"app": {"port": 8080}})
    assert res["restart_fields"] == ["app.port"]
    assert res["restart_required"] is True


# --------------------------------------------------------------------------- #
# structured validation errors (no silent coercion)
# --------------------------------------------------------------------------- #
def test_duplicate_client_id_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config(
            {"xtquant": {"clients": [{"client_id": "dup"}, {"client_id": "dup"}]}}
        )
    assert ei.value.field == "xtquant.clients"
    assert ei.value.error_code == "invalid_config"
    assert "重复" in ei.value.message


def test_client_missing_client_id_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"xtquant": {"clients": [{"name": "no-id"}]}})
    assert ei.value.field == "xtquant.clients"


def test_invalid_mode_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"xtquant": {"mode": "bogus"}})
    assert ei.value.field == "xtquant.mode"


def test_invalid_port_type_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"app": {"port": "8080"}})  # 字符串端口 → 拒绝
    assert ei.value.field == "app.port"


def test_invalid_port_range_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"grpc": {"port": 70000}})
    assert ei.value.field == "grpc.port"


def test_non_bool_allow_real_trading_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"xtquant": {"trading": {"allow_real_trading": "yes"}}})
    assert ei.value.field == "xtquant.trading.allow_real_trading"


def test_non_object_section_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError) as ei:
        cs.write_config({"xtquant": "not-a-dict"})
    assert ei.value.field == "xtquant"


def test_patch_not_dict_raises(cfg_path):
    with pytest.raises(cs.ConfigStoreError):
        cs.write_config([1, 2, 3])  # type: ignore[arg-type]
