"""Upsert instrument_catalog rows from akshare listing tables."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from doyoutrade.data.instrument_universe.akshare_a import _sync_fetch_spot_rows
from doyoutrade.data.instrument_catalog.normalize import canonical_symbol_from_doyoutrade_or_akshare
from doyoutrade.data.instrument_catalog.index_seeds import index_seed_rows

if TYPE_CHECKING:
    from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def sync_akshare_catalog(
    repo: "SqlAlchemyInstrumentCatalogRepository",
    *,
    mode: str,
    symbols: list[str] | None = None,
) -> dict[str, int]:
    """Import A-share listings via the same sync path as instrument-universe search.

    ``mode`` is ``full`` (all rows) or ``symbols`` (subset by canonical symbol).
    """
    rows = await asyncio.to_thread(_sync_fetch_spot_rows)
    want = None
    if mode == "symbols":
        want = {canonical_symbol_from_doyoutrade_or_akshare(s) for s in (symbols or []) if str(s).strip()}
        want |= {str(s).strip().upper() for s in (symbols or []) if "." in str(s)}
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol") or ""
        if not sym:
            continue
        if want is not None and sym not in want:
            continue
        # ``instrument_type`` comes from the listing fetcher: "stock" for the
        # A-share / BJ code tables, "etf" for the fund_etf_spot_em rows. Both
        # are on-exchange tradable (ETF sells are stamp-tax exempt, handled at
        # the fee layer). Fall back to "stock" for legacy rows without a type.
        instrument_type = str(r.get("instrument_type") or "stock")
        out_rows.append(
            {
                "symbol": sym,
                "display_name": r.get("name") or "",
                "market": r.get("market") or "CN",
                "instrument_type": instrument_type,
                "is_tradable": True,
                "last_sync_source": "akshare",
                "last_sync_at": _utcnow(),
                "raw": {"source": "akshare", "listing_row": r},
            }
        )
    # akshare 的现货列表只含股票；指数从不出现在里面，需用内置种子补齐，
    # 否则自选股页面永远搜不到上证指数等指数 (full sync 才补，symbols 模式不动)。
    if mode == "full":
        out_rows.extend(index_seed_rows(source="akshare"))
    inserted, updated = await repo.upsert_rows(out_rows)
    return {"inserted": inserted, "updated": updated, "rows_seen": len(out_rows)}
