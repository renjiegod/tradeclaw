from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIBS_ROOT = PROJECT_ROOT.parent  # canonical qmt_proxy_sdk now lives at monorepo root

if str(LIBS_ROOT) not in sys.path:
    sys.path.insert(0, str(LIBS_ROOT))

from qmt_proxy_sdk.models.data import QuoteData

import examples.ma_crossover_strategy as ma_strategy
from examples.ma_crossover_strategy import format_connect_failure_message, format_tick_log_line


def test_format_tick_log_line_includes_price_change_amount_and_volume():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
        pre_close=1220.0,
        amount=4567890.0,
        volume=1200,
    )

    line = format_tick_log_line(
        tick_count=1,
        quote=quote,
        short_ma=1230.12,
        long_ma=1218.34,
        position_str="无",
    )

    assert "[TICK #0001] 600519.SH" in line
    assert "价格=1234.56" in line
    assert "涨跌幅=1.19%" in line
    assert "成交额=4567890.00" in line
    assert "量=1200" in line
    assert "MA5=1230.12" in line
    assert "MA20=1218.34" in line
    assert "持仓=无" in line


def test_format_tick_log_line_uses_na_for_missing_optional_fields():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
    )

    line = format_tick_log_line(
        tick_count=2,
        quote=quote,
        short_ma=None,
        long_ma=None,
        position_str="无",
    )

    assert "涨跌幅=N/A" in line
    assert "成交额=N/A" in line
    assert "量=N/A" in line
    assert "MA5=N/A" in line
    assert "MA20=N/A" in line


def test_format_tick_log_line_keeps_zero_amount_and_volume():
    quote = QuoteData(
        stock_code="600519.SH",
        last_price=1234.56,
        pre_close=0,
        amount=0,
        volume=0,
    )

    line = format_tick_log_line(
        tick_count=3,
        quote=quote,
        short_ma=1230.0,
        long_ma=1218.0,
        position_str="100股",
    )

    assert "涨跌幅=N/A" in line
    assert "成交额=0.00" in line
    assert "量=0" in line
    assert "持仓=100股" in line


def test_format_connect_failure_message_explains_placeholder_account_subscription_error():
    message = format_connect_failure_message("test_account", "订阅交易账户失败，返回码: -1")

    assert "订阅交易账户失败，返回码: -1" in message
    assert "QMT_ACCOUNT_ID" in message
    assert "真实账户" in message


def test_format_connect_failure_message_keeps_non_placeholder_errors_unchanged():
    message = format_connect_failure_message("acct-001", "xttrader 未初始化或未连接")

    assert message == "交易连接失败: xttrader 未初始化或未连接"


def test_resolve_runtime_settings_reads_values_from_example_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("QMT_PROXY_URL", raising=False)
    monkeypatch.delenv("QMT_API_KEY", raising=False)
    monkeypatch.delenv("QMT_ACCOUNT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QMT_PROXY_URL=http://env-file:9000\n"
        "QMT_API_KEY=env-file-key\n"
        "QMT_ACCOUNT_ID=env-file-account\n",
        encoding="utf-8",
    )

    assert hasattr(ma_strategy, "resolve_runtime_settings")

    settings = ma_strategy.resolve_runtime_settings(env_path=env_file)

    assert settings["base_url"] == "http://env-file:9000"
    assert settings["api_key"] == "env-file-key"
    assert settings["account_id"] == "env-file-account"


def test_resolve_runtime_settings_prefers_existing_environment_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QMT_PROXY_URL", "http://from-env:8000")
    monkeypatch.setenv("QMT_API_KEY", "shell-key")
    monkeypatch.setenv("QMT_ACCOUNT_ID", "shell-account")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QMT_PROXY_URL=http://env-file:9000\n"
        "QMT_API_KEY=env-file-key\n"
        "QMT_ACCOUNT_ID=env-file-account\n",
        encoding="utf-8",
    )

    assert hasattr(ma_strategy, "resolve_runtime_settings")

    settings = ma_strategy.resolve_runtime_settings(env_path=env_file)

    assert settings["base_url"] == "http://from-env:8000"
    assert settings["api_key"] == "shell-key"
    assert settings["account_id"] == "shell-account"
