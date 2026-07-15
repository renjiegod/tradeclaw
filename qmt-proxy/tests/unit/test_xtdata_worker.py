"""Tests for xtdata worker subprocess IPC."""
import io
import os
import pickle
import subprocess
import sys

from app.utils import xtdata_worker


class _FakeStdio:
    def __init__(self, buffer: io.BytesIO):
        self.buffer = buffer


def test_isolated_stdout_suppresses_fd_writes():
    read_fd, write_fd = os.pipe()
    saved_stdout = os.dup(1)
    os.dup2(write_fd, 1)
    os.close(write_fd)

    try:
        with xtdata_worker._isolated_stdout():
            os.write(1, b"xtquant noise")
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)

    captured = os.read(read_fd, 1024)
    os.close(read_fd)
    assert captured == b""


def test_main_returns_pickled_result_despite_stray_stdout(monkeypatch):
    read_fd, write_fd = os.pipe()
    saved_stdout = os.dup(1)
    os.dup2(write_fd, 1)
    os.close(write_fd)

    def fake_execute(_payload):
        os.write(1, b"\nnoisy xtquant output\n")
        return {"field": "value"}

    monkeypatch.setattr(xtdata_worker, "_execute", fake_execute)
    payload = pickle.dumps({"operation": "get_market_data", "kwargs": {}})
    monkeypatch.setattr(sys, "stdin", _FakeStdio(io.BytesIO(payload)))

    try:
        exit_code = xtdata_worker.main()
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)

    output = os.read(read_fd, 65536)
    os.close(read_fd)

    assert exit_code == 0
    response = pickle.loads(output)
    assert response == {"ok": True, "result": {"field": "value"}}


def test_main_returns_pickled_error_on_failure(monkeypatch):
    read_fd, write_fd = os.pipe()
    saved_stdout = os.dup(1)
    os.dup2(write_fd, 1)
    os.close(write_fd)

    def fake_execute(_payload):
        raise RuntimeError("xtdata boom")

    monkeypatch.setattr(xtdata_worker, "_execute", fake_execute)
    payload = pickle.dumps({"operation": "get_market_data", "kwargs": {}})
    monkeypatch.setattr(sys, "stdin", _FakeStdio(io.BytesIO(payload)))

    try:
        exit_code = xtdata_worker.main()
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)

    output = os.read(read_fd, 65536)
    os.close(read_fd)

    assert exit_code == 0
    response = pickle.loads(output)
    assert response["ok"] is False
    assert "xtdata boom" in response["error"]


class _FakeXtdata:
    def __init__(self):
        self.download_calls = []
        self.get_calls = []

    def download_history_data(self, **kwargs):
        self.download_calls.append(kwargs)

    def get_market_data(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"sentinel": "market-data"}

    def get_local_data(self, **kwargs):
        return {"sentinel": "local-data"}


def test_execute_routes_download_and_get_market_data(monkeypatch):
    fake = _FakeXtdata()
    monkeypatch.setattr(xtdata_worker, "_configure_xtdata", lambda _p: fake)

    payload = {
        "operation": "download_and_get_market_data",
        "kwargs": {
            "download": {"stock_code": "000001.SZ", "period": "1d"},
            "market": {"stock_list": ["000001.SZ"], "period": "1d", "count": -1},
        },
        "qmt_userdata_path": None,
    }

    result = xtdata_worker._execute(payload)

    # Single process performed both the download and the read, in order.
    assert fake.download_calls == [{"stock_code": "000001.SZ", "period": "1d"}]
    assert fake.get_calls == [{"stock_list": ["000001.SZ"], "period": "1d", "count": -1}]
    assert result == {"sentinel": "market-data"}


def test_execute_download_and_get_tolerates_missing_subkeys(monkeypatch):
    fake = _FakeXtdata()
    monkeypatch.setattr(xtdata_worker, "_configure_xtdata", lambda _p: fake)

    payload = {
        "operation": "download_and_get_market_data",
        "kwargs": {},
        "qmt_userdata_path": None,
    }

    result = xtdata_worker._execute(payload)
    assert fake.download_calls == [{}]
    assert fake.get_calls == [{}]
    assert result == {"sentinel": "market-data"}


def test_execute_legacy_operations_still_routed(monkeypatch):
    fake = _FakeXtdata()
    monkeypatch.setattr(xtdata_worker, "_configure_xtdata", lambda _p: fake)

    assert xtdata_worker._execute(
        {"operation": "get_market_data", "kwargs": {"stock_list": ["x"]}}
    ) == {"sentinel": "market-data"}
    xtdata_worker._execute({"operation": "download_history_data", "kwargs": {"stock_code": "x"}})
    assert fake.download_calls == [{"stock_code": "x"}]


def test_execute_unknown_operation_raises(monkeypatch):
    monkeypatch.setattr(xtdata_worker, "_configure_xtdata", lambda _p: _FakeXtdata())
    import pytest

    with pytest.raises(ValueError, match="Unsupported xtdata operation"):
        xtdata_worker._execute({"operation": "bogus", "kwargs": {}})


def test_subprocess_returns_pickled_error_for_unknown_operation():
    completed = subprocess.run(
        [sys.executable, "-m", "app.utils.xtdata_worker"],
        input=pickle.dumps({"operation": "bogus", "kwargs": {}}),
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    response = pickle.loads(completed.stdout)
    assert response["ok"] is False
    assert "Unsupported xtdata operation" in response["error"]
