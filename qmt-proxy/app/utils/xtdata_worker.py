"""Run xtdata calls in an isolated process (invoked via python -m app.utils.xtdata_worker)."""
from __future__ import annotations

import contextlib
import os
import pickle
import sys
from typing import Any, Iterator


@contextlib.contextmanager
def _isolated_stdout() -> Iterator[None]:
    """Suppress stray writes to stdout from xtquant while keeping stderr."""
    stdout_fd = os.dup(1)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 1)
        yield
    finally:
        os.dup2(stdout_fd, 1)
        os.close(stdout_fd)
        os.close(devnull_fd)


def _configure_xtdata(qmt_userdata_path: str | None):
    import xtquant.xtdata as xtdata

    xtdata.enable_hello = False
    if qmt_userdata_path:
        xtdata.data_dir = os.path.join(qmt_userdata_path, "datadir")
    xtdata.connect()
    return xtdata


def _execute(payload: dict[str, Any]) -> Any:
    operation = payload["operation"]
    kwargs = payload.get("kwargs", {})
    xtdata = _configure_xtdata(payload.get("qmt_userdata_path"))

    if operation == "download_history_data":
        xtdata.download_history_data(**kwargs)
        return None
    if operation == "get_market_data":
        return xtdata.get_market_data(**kwargs)
    if operation == "get_local_data":
        return xtdata.get_local_data(**kwargs)
    if operation == "download_and_get_market_data":
        # Combine download + get into one subprocess so we pay the xtdata.connect()
        # cost only once instead of spawning two separate worker processes.
        download_kwargs = kwargs.get("download", {})
        market_kwargs = kwargs.get("market", {})
        xtdata.download_history_data(**download_kwargs)
        return xtdata.get_market_data(**market_kwargs)

    raise ValueError(f"Unsupported xtdata operation: {operation}")


def _write_response(response: dict[str, Any]) -> None:
    os.write(1, pickle.dumps(response))


def main() -> int:
    payload = pickle.loads(sys.stdin.buffer.read())
    try:
        with _isolated_stdout():
            result = _execute(payload)
        response: dict[str, Any] = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}

    _write_response(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
