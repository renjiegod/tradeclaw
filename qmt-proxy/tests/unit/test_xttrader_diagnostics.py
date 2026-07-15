"""Tests for xttrader connection failure diagnostics."""

from pathlib import Path

from app.utils.xttrader_diagnostics import (
    build_xttrader_connect_diagnostics,
    describe_xttrader_return_code,
    format_xttrader_operation_failure,
)


def test_describe_xttrader_return_code_minus_one():
    message = describe_xttrader_return_code(-1)

    assert "返回码 -1" in message
    assert "通信失败" in message


def test_build_diagnostics_flags_missing_path(tmp_path: Path):
    missing = tmp_path / "missing" / "userdata_mini"

    lines = build_xttrader_connect_diagnostics(
        str(missing),
        connect_result=-1,
        trader_session=42,
    )

    joined = "\n".join(lines)
    assert "目录不存在" in joined
    assert "session_id: 42" in joined
    assert "QMT/MiniQMT 未启动" in joined


def test_build_diagnostics_detects_userdata_mini_and_missing_api_permission_file(tmp_path: Path):
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()

    lines = build_xttrader_connect_diagnostics(
        str(userdata),
        connect_result=-1,
    )

    joined = "\n".join(lines)
    assert "目录存在" in joined
    assert "userdata_mini" in joined
    assert "up_queue_xtquant" in joined


def test_build_diagnostics_warns_about_spaces_and_non_ascii():
    path = r"C:\测试券商 QMT\userdata_mini"

    lines = build_xttrader_connect_diagnostics(path, connect_result=-1)

    joined = "\n".join(lines)
    assert "含空格" in joined
    assert "非 ASCII" in joined


def test_format_xttrader_operation_failure_includes_summary_and_diagnostics(tmp_path: Path):
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()

    message = format_xttrader_operation_failure(
        -1,
        operation="连接",
        qmt_userdata_path=str(userdata),
        trader_session=99,
    )

    assert "xttrader 连接失败" in message
    assert "返回码 -1" in message
    assert str(userdata) in message
    assert "session_id: 99" in message


def test_format_xttrader_operation_failure_without_path_keeps_summary():
    message = format_xttrader_operation_failure(-1, operation="订阅交易账户", account_id="acct-001")

    assert "订阅交易账户失败" in message
    assert "返回码 -1" in message
    assert "account_id: acct-001" in message
    assert "排查信息" not in message
