"""Execute crash-prone xtdata calls in a subprocess to protect the API server."""
from __future__ import annotations

import pickle
import subprocess
import sys
import time
from typing import Any, Optional

from app.utils import diagnostics
from app.utils.exceptions import DataServiceException
from app.utils.logger import logger

_NATIVE_CRASH_EXIT_CODES = {
    -1073740791,  # STATUS_STACK_BUFFER_OVERRUN
    -1073741819,  # STATUS_ACCESS_VIOLATION
    3221226505,
    3221225477,
}


def is_native_crash(exit_code: int | None) -> bool:
    if exit_code is None:
        return False
    if exit_code in _NATIVE_CRASH_EXIT_CODES:
        return True
    if exit_code < 0 or exit_code > 128:
        return True
    return False


def run_xtdata_operation(
    operation: str,
    kwargs: dict[str, Any],
    *,
    qmt_userdata_path: Optional[str] = None,
    client_id: str = "default",
    timeout: float = 120.0,
) -> Any:
    """Run an xtdata operation in a child process and return its result.

    ``client_id`` 仅用于诊断打标（区分该取数落在哪个 QMT 终端）；实际数据目录由
    ``qmt_userdata_path`` 决定（行情与券商无关，通常即数据源终端的 datadir）。
    """
    payload = {
        "operation": operation,
        "kwargs": kwargs,
        "qmt_userdata_path": qmt_userdata_path,
        "client_id": client_id,
    }
    command = [sys.executable, "-m", "app.utils.xtdata_worker"]

    logger.debug(f"Running isolated xtdata operation: {operation} (client_id={client_id})")

    started = time.monotonic()

    def _elapsed_ms() -> float:
        return (time.monotonic() - started) * 1000.0

    def _record(
        *,
        ok: bool,
        error: Optional[str] = None,
        exit_code: Optional[int] = None,
        stderr_snippet: Optional[str] = None,
    ) -> None:
        diagnostics.record(
            operation=operation,
            kwargs=kwargs,
            duration_ms=_elapsed_ms(),
            ok=ok,
            client_id=client_id,
            error=error,
            exit_code=exit_code,
            stderr_snippet=stderr_snippet,
        )

    try:
        completed = subprocess.run(
            command,
            input=pickle.dumps(payload),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore").strip() if exc.stderr else ""
        message = f"xtdata 子进程超时 [operation={operation}, timeout={timeout}s]"
        logger.error(message)
        _record(ok=False, error=message, exit_code=None, stderr_snippet=stderr)
        raise DataServiceException(message) from exc

    if is_native_crash(completed.returncode):
        stderr = completed.stderr.decode("utf-8", errors="ignore").strip()
        logger.error(
            f"xtdata 子进程原生崩溃 [operation={operation}, exit={completed.returncode}]: {stderr}"
        )
        message = (
            "xtquant 原生层崩溃（常见于 K 线数据下载/读取）。"
            "请确认 QMT 已启动、xtquant 与 QMT 版本一致，或在查询时启用 disable_download。"
            f" operation={operation}"
        )
        _record(
            ok=False,
            error=message,
            exit_code=completed.returncode,
            stderr_snippet=stderr,
        )
        raise DataServiceException(message)

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="ignore").strip()
        message = (
            f"xtdata 子进程失败 [operation={operation}, exit={completed.returncode}]: {stderr}"
        )
        _record(
            ok=False,
            error=message,
            exit_code=completed.returncode,
            stderr_snippet=stderr,
        )
        raise DataServiceException(message)

    try:
        response = pickle.loads(completed.stdout)
    except Exception as exc:
        message = f"xtdata 子进程返回无效结果: {exc}"
        _record(
            ok=False,
            error=message,
            exit_code=completed.returncode,
            stderr_snippet=completed.stderr.decode("utf-8", errors="ignore").strip(),
        )
        raise DataServiceException(message) from exc

    if not response.get("ok"):
        error = response.get("error", "xtdata 子进程返回失败")
        _record(
            ok=False,
            error=error,
            exit_code=completed.returncode,
            stderr_snippet=completed.stderr.decode("utf-8", errors="ignore").strip(),
        )
        raise DataServiceException(error)

    _record(ok=True, exit_code=completed.returncode)
    return response.get("result")
