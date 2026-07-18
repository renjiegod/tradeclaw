"""强势股时间线 CSV — 知识库确定性来源读取器。

读 ``cycles/_strong_timeline.csv``（规范名，优先）或遗留的
``cycles/强势股时间线.csv``。解析结果供图谱确定性投影使用：每行是一只
标的的一轮主升波次（同代码多波 = 多行）。

列契约（表头必须匹配，顺序固定）::

    代码,名称,启动日,启动价(前复权),关注日,主升期望卖点,高点日,高点价(前复权),
    最高涨幅%,最晚行情结束日(...),拉升交易日(...),整段日历天(...),题材(待核),说明,标签

纪律：

- 脏行（缺代码 / 非法 symbol / 缺启动日）进 ``warnings``，不静默丢弃。
- ``主升期望卖点`` 允许 ``日期 + 中文逗号叙事`` 同字段（中文逗号不拆列）。
- 「未退潮 / 进行中」结束日 → ``ongoing=True``，``end_date=None``。
- 本模块只读，不写盘、不改 ontology。
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 规范名优先；中文文件名是用户已有遗留路径。
TIMELINE_CANDIDATE_RELPATHS: tuple[str, ...] = (
    "cycles/_strong_timeline.csv",
    "cycles/强势股时间线.csv",
)

_REQUIRED_HEADERS: tuple[str, ...] = (
    "代码",
    "名称",
    "启动日",
    "启动价(前复权)",
    "关注日",
    "主升期望卖点",
    "高点日",
    "高点价(前复权)",
    "最高涨幅%",
    "最晚行情结束日(在真正退潮日后面)",
    "拉升交易日(启动→高点)",
    "整段日历天(启动→结束)",
    "题材(待核)",
    "说明",
    "标签",
)

_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ONGOING_MARKERS = ("未退潮", "进行中")


def _resolve_timeline_path(root: Path) -> tuple[Path, str] | None:
    for relpath in TIMELINE_CANDIDATE_RELPATHS:
        path = root / relpath
        if path.is_file():
            return path, relpath
    return None


def _is_iso_date(value: str) -> bool:
    return bool(_DATE_RE.match(value))


def _parse_end_date(raw: str) -> tuple[str | None, bool]:
    text = raw.strip()
    if not text:
        return None, False
    if any(marker in text for marker in _ONGOING_MARKERS):
        return None, True
    if _is_iso_date(text):
        return text, False
    # 允许「约 2022-09-30」这类前缀：抽第一段 YYYY-MM-DD。
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0), False
    return None, False


def _parse_row(
    row: dict[str, str],
    *,
    line_number: int,
    relpath: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    symbol = (row.get("代码") or "").strip()
    if not symbol or not _SYMBOL_RE.match(symbol):
        warning = {
            "source": f"kb:{relpath}",
            "reason": "timeline_row_bad_symbol",
            "line_number": line_number,
            "raw_symbol": symbol,
        }
        logger.info(
            "strong timeline skipping row reason=bad_symbol line=%s symbol=%r",
            line_number,
            symbol,
        )
        return None, warning

    start_date = (row.get("启动日") or "").strip()
    if not _is_iso_date(start_date):
        warning = {
            "source": f"kb:{relpath}",
            "reason": "timeline_row_bad_start_date",
            "line_number": line_number,
            "symbol": symbol,
            "raw_start_date": start_date,
        }
        logger.info(
            "strong timeline skipping row reason=bad_start_date line=%s symbol=%r",
            line_number,
            symbol,
        )
        return None, warning

    end_raw = (row.get("最晚行情结束日(在真正退潮日后面)") or "").strip()
    end_date, ongoing = _parse_end_date(end_raw)

    peak_date = (row.get("高点日") or "").strip()
    if peak_date and not _is_iso_date(peak_date):
        peak_date = ""

    watch_date = (row.get("关注日") or "").strip()
    if watch_date and not _is_iso_date(watch_date):
        watch_date = ""

    name = (row.get("名称") or "").strip()
    theme = (row.get("题材(待核)") or "").strip()
    role = (row.get("标签") or "").strip()
    note = (row.get("说明") or "").strip()
    sell_target = (row.get("主升期望卖点") or "").strip()
    rally_trading_days = (row.get("拉升交易日(启动→高点)") or "").strip() or None
    calendar_days = (row.get("整段日历天(启动→结束)") or "").strip() or None
    # 部分行把「进行中」写在日历天列、结束日留空（如利通电子）。
    if not ongoing and calendar_days and any(
        marker in calendar_days for marker in _ONGOING_MARKERS
    ):
        ongoing = True

    item: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "start_date": start_date,
        "start_price": (row.get("启动价(前复权)") or "").strip() or None,
        "watch_date": watch_date or None,
        "sell_target": sell_target or None,
        "peak_date": peak_date or None,
        "peak_price": (row.get("高点价(前复权)") or "").strip() or None,
        "max_gain_pct": (row.get("最高涨幅%") or "").strip() or None,
        "end_date": end_date,
        "ongoing": ongoing,
        "rally_trading_days": rally_trading_days,
        "calendar_days": calendar_days,
        "theme": theme,
        "note": note,
        "role": role,
        "line_number": line_number,
        "relpath": relpath,
    }
    return item, None


def read_strong_timeline(*, root: Path) -> dict[str, Any]:
    """Read the strong-stock timeline CSV under ``root``.

    Returns ``{items, warnings, relpath}``. ``relpath`` is ``None`` when no
    candidate file exists (not an error — fresh envs simply have no timeline).
    """
    resolved = _resolve_timeline_path(root)
    if resolved is None:
        return {"items": [], "warnings": [], "relpath": None}

    path, relpath = resolved
    warnings: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                warnings.append(
                    {
                        "source": f"kb:{relpath}",
                        "reason": "timeline_empty_or_unreadable",
                    }
                )
                return {"items": [], "warnings": warnings, "relpath": relpath}

            missing = [h for h in _REQUIRED_HEADERS if h not in reader.fieldnames]
            if missing:
                warnings.append(
                    {
                        "source": f"kb:{relpath}",
                        "reason": "timeline_header_mismatch",
                        "missing_headers": missing,
                        "actual_headers": list(reader.fieldnames),
                    }
                )
                logger.info(
                    "strong timeline header mismatch path=%s missing=%s",
                    relpath,
                    missing,
                )
                return {"items": [], "warnings": warnings, "relpath": relpath}

            for offset, row in enumerate(reader, start=2):
                # DictReader yields None values for short rows; normalise.
                normalised = {
                    key: (value if value is not None else "")
                    for key, value in row.items()
                    if key is not None
                }
                # Skip fully blank trailing rows.
                if not any(str(v).strip() for v in normalised.values()):
                    continue
                item, warning = _parse_row(
                    normalised, line_number=offset, relpath=relpath
                )
                if warning is not None:
                    warnings.append(warning)
                if item is not None:
                    items.append(item)
    except OSError as exc:
        warnings.append(
            {
                "source": f"kb:{relpath}",
                "reason": "timeline_read_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        logger.info("strong timeline read failed path=%s err=%s", relpath, exc)
        return {"items": [], "warnings": warnings, "relpath": relpath}

    return {"items": items, "warnings": warnings, "relpath": relpath}


__all__ = [
    "TIMELINE_CANDIDATE_RELPATHS",
    "read_strong_timeline",
]
