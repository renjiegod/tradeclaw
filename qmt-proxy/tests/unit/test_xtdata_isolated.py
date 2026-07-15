"""Tests for isolated xtdata subprocess runner."""
import pickle
from types import SimpleNamespace

import pytest

from app.utils import xtdata_isolated
from app.utils.exceptions import DataServiceException


def test_is_native_crash_detects_windows_fast_fail():
    assert xtdata_isolated.is_native_crash(-1073740791)
    assert xtdata_isolated.is_native_crash(3221226505)
    assert not xtdata_isolated.is_native_crash(0)
    assert not xtdata_isolated.is_native_crash(1)


def test_run_operation_raises_when_subprocess_crashes(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=-1073740791, stdout=b"", stderr=b"bson assert")

    monkeypatch.setattr(xtdata_isolated.subprocess, "run", fake_run)

    with pytest.raises(DataServiceException, match="原生层崩溃"):
        xtdata_isolated.run_xtdata_operation(
            "get_market_data",
            {"field_list": [], "stock_list": ["000001.SZ"]},
            qmt_userdata_path=r"C:\QMT\userdata_mini",
        )


def test_run_operation_returns_unpickled_result(monkeypatch):
    payload = {"ok": True, "result": {"time": "mock"}}

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=pickle.dumps(payload), stderr=b"")

    monkeypatch.setattr(xtdata_isolated.subprocess, "run", fake_run)

    result = xtdata_isolated.run_xtdata_operation(
        "get_market_data",
        {"field_list": [], "stock_list": ["000001.SZ"]},
        qmt_userdata_path=None,
    )
    assert result == {"time": "mock"}


def test_run_operation_records_diagnostics_on_success(monkeypatch):
    from app.utils import diagnostics

    diagnostics._reset_for_tests()
    monkeypatch.setattr(diagnostics, "_JSONL_PATH", "/tmp/qmt_test_ops_success.jsonl")

    payload = {"ok": True, "result": {"time": "mock"}}

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=pickle.dumps(payload), stderr=b"")

    monkeypatch.setattr(xtdata_isolated.subprocess, "run", fake_run)

    xtdata_isolated.run_xtdata_operation(
        "download_and_get_market_data",
        {"download": {"stock_code": "000001.SZ"}, "market": {"stock_list": ["000001.SZ"]}},
        qmt_userdata_path=None,
    )

    records = diagnostics.recent(limit=5)
    assert len(records) == 1
    assert records[0]["operation"] == "download_and_get_market_data"
    assert records[0]["ok"] is True
    assert records[0]["exit_code"] == 0
    assert records[0]["duration_ms"] >= 0
    diagnostics._reset_for_tests()


def test_run_operation_records_diagnostics_on_native_crash(monkeypatch):
    from app.utils import diagnostics

    diagnostics._reset_for_tests()
    monkeypatch.setattr(diagnostics, "_JSONL_PATH", "/tmp/qmt_test_ops_crash.jsonl")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=-1073740791, stdout=b"", stderr=b"bson assert")

    monkeypatch.setattr(xtdata_isolated.subprocess, "run", fake_run)

    with pytest.raises(DataServiceException):
        xtdata_isolated.run_xtdata_operation(
            "get_market_data",
            {"stock_list": ["000001.SZ"]},
            qmt_userdata_path=None,
        )

    records = diagnostics.recent(limit=5)
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["exit_code"] == -1073740791
    assert "bson assert" in (records[0]["stderr_snippet"] or "")
    diagnostics._reset_for_tests()


def test_market_data_request_defaults_disable_download():
    from app.models.data_models import MarketDataRequest

    request = MarketDataRequest(stock_codes=["000001.SZ"])
    assert request.disable_download is True
