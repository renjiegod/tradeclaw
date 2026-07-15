"""Search the locally-synced ``instrument_catalog`` table.

Same query semantics as the frontend ``/stocks`` page: ``symbol`` prefix
match, substring match against ``display_name``, plus pinyin and initials
for ASCII-only queries. Returns rows in the
same ``{symbol, name, market}`` shape as ``search_akshare_a`` so callers
don't care which source produced them.

Unlike ``search_akshare_a`` this never hits akshare, so it incurs no
network round-trip and no tqdm progress bar. The catalog has to be
populated first (via ``TradingPlatformService.sync_instrument_catalog``
or the frontend ``/stocks`` sync action).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository


async def search_local_catalog(
    *,
    repository: "SqlAlchemyInstrumentCatalogRepository",
    q: str,
    limit: int,
) -> List[Dict[str, str]]:
    q_stripped = (q or "").strip()
    if not q_stripped:
        return []
    page = await repository.list_page(q=q_stripped, limit=limit, offset=0)
    rows = page[0]
    items: List[Dict[str, str]] = []
    for row in rows:
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        items.append(
            {
                "symbol": sym,
                "name": str(row.get("display_name") or ""),
                "market": str(row.get("market") or ""),
            }
        )
    return items
