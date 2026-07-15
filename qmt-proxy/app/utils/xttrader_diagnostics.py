"""Diagnostics helpers for xttrader connection and subscription failures."""

from __future__ import annotations

import os
from typing import Optional

# 迅投知识库: connect()/subscribe() 返回 0 表示成功，-1 表示失败。
_XTTRADER_RETURN_CODE_HINTS: dict[int, str] = {
    0: "连接成功",
    -1: "与本地 QMT 交易服务通信失败",
}

_CONNECT_MINUS_ONE_HINTS: tuple[str, ...] = (
    "QMT/MiniQMT 未启动，或未登录交易账户",
    "qmt_userdata_path 与当前运行的客户端不匹配",
    "同一 session 连续 connect 间隔不足 3 秒",
    "防火墙/杀毒软件拦截本地 IPC",
    "xtquant 库版本与 QMT 客户端不一致",
)


def describe_xttrader_return_code(code: int) -> str:
    """Return a short human-readable description for an xttrader result code."""
    hint = _XTTRADER_RETURN_CODE_HINTS.get(code)
    if hint:
        return f"返回码 {code}（{hint}）"
    return f"返回码 {code}"


def _path_has_non_ascii(path: str) -> bool:
    try:
        path.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def build_xttrader_connect_diagnostics(
    qmt_userdata_path: str,
    *,
    connect_result: int,
    trader_session: Optional[int] = None,
) -> list[str]:
    """Build diagnostic lines for a failed xttrader connect attempt."""
    lines: list[str] = []
    norm_path = os.path.normpath(qmt_userdata_path)

    lines.append(f"配置路径: {qmt_userdata_path}")

    if os.path.isdir(norm_path):
        lines.append("路径状态: 目录存在")
    elif os.path.exists(norm_path):
        lines.append("路径状态: 存在但不是目录")
    else:
        lines.append("路径状态: 目录不存在")

    basename = os.path.basename(norm_path.rstrip("\\/")).lower()
    if basename == "userdata_mini":
        lines.append("路径类型: userdata_mini（MiniQMT）")
    elif basename == "userdata":
        lines.append("路径类型: userdata（标准/投研端）")
    else:
        lines.append(
            "路径类型: 未识别为 userdata_mini 或 userdata，"
            "MiniQMT 通常应指向安装目录下的 userdata_mini"
        )

    if " " in norm_path:
        lines.append("路径风险: 含空格，可能导致连接失败")

    if _path_has_non_ascii(norm_path):
        lines.append("路径风险: 含非 ASCII 字符（如中文），可能导致连接失败")

    up_queue = os.path.join(norm_path, "up_queue_xtquant")
    if os.path.isfile(up_queue):
        lines.append("API 权限文件: up_queue_xtquant 存在")
    elif os.path.isdir(norm_path):
        lines.append(
            "API 权限文件: userdata 内无 up_queue_xtquant，"
            "可能未开通 MiniQMT API 交易权限，需联系券商"
        )

    if trader_session is not None:
        lines.append(f"trader session_id: {trader_session}")

    if connect_result == -1:
        lines.append("常见原因:")
        for index, hint in enumerate(_CONNECT_MINUS_ONE_HINTS, start=1):
            lines.append(f"  {index}. {hint}")

    return lines


def format_xttrader_operation_failure(
    result_code: int,
    *,
    operation: str,
    qmt_userdata_path: Optional[str] = None,
    trader_session: Optional[int] = None,
    account_id: Optional[str] = None,
) -> str:
    """Format a detailed xttrader failure message for logs and API responses."""
    lines = [f"xttrader {operation}失败，{describe_xttrader_return_code(result_code)}"]

    if account_id is not None:
        lines.append(f"account_id: {account_id}")

    if qmt_userdata_path:
        lines.append("排查信息:")
        for item in build_xttrader_connect_diagnostics(
            qmt_userdata_path,
            connect_result=result_code,
            trader_session=trader_session,
        ):
            lines.append(f"  - {item}")

    return "\n".join(lines)
