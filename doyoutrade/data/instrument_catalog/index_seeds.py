"""Built-in seed rows for common A-share **indices** (上证指数 等).

The akshare / QMT sector listings used by the catalog sync paths only contain
**stocks** (``is_cn_a_share_equity_symbol`` 主动排除指数/ETF/债券)，所以指数永远
不会进 ``instrument_catalog``，自选股页面也就搜不到、加不进去。

指数没有一个稳定的"全市场列表"接口可走，而常用指数是一组小而稳定的已知集合，
因此这里以静态种子的形式维护，并在每次 full catalog sync 末尾 upsert 进去，
``instrument_type`` 明确标记为 ``"index"`` 且 ``is_tradable=False`` —— 与股票
区分，让下游（market 数据 sync、交易 universe 校验等）能按类型把指数挡在
可交易范围之外（CLAUDE.md §错误可见性：失败模式 / 类型必须结构化区分）。

canonical symbol 采用 QMT 风格 ``<code>.<EXCH>``，与 stock 行一致。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# (canonical_symbol, display_name)。行业标准指数代码：上证系列 .SH（000xxx），
# 深证系列 .SZ（399xxx），北证 50 为 .BJ。
A_SHARE_INDEX_SEEDS: list[tuple[str, str]] = [
    ("000001.SH", "上证指数"),
    ("000300.SH", "沪深300"),
    ("000016.SH", "上证50"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("000688.SH", "科创50"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("399005.SZ", "中小100"),
    ("899050.BJ", "北证50"),
]

# Public so callers can fast-path "is this a known index?" without a DB round-trip.
A_SHARE_INDEX_SYMBOLS: frozenset[str] = frozenset(sym for sym, _ in A_SHARE_INDEX_SEEDS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def index_seed_rows(*, source: str) -> list[dict[str, Any]]:
    """Return ``instrument_catalog`` upsert rows for the common A-share indices.

    ``source`` is recorded in ``last_sync_source`` (e.g. ``"akshare+index_seed"``)
    so an operator can tell these rows came from the built-in seed rather than a
    live vendor listing. ``is_tradable=False`` is the load-bearing flag that keeps
    indices out of tradable universes downstream.
    """
    now = _utcnow()
    rows: list[dict[str, Any]] = []
    for symbol, name in A_SHARE_INDEX_SEEDS:
        rows.append(
            {
                "symbol": symbol,
                "display_name": name,
                "market": "CN",
                "instrument_type": "index",
                "is_tradable": False,
                "last_sync_source": f"{source}+index_seed",
                "last_sync_at": now,
                "raw": {"source": "index_seed", "name": name},
            }
        )
    return rows
