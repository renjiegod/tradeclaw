"""Registry of instrument-universe search sources (query param `source`).

Two sources are supported:

- ``local_catalog`` — query the locally-synced ``instrument_catalog`` table.
  Zero network, zero progress-bar noise. Requires an
  ``SqlAlchemyInstrumentCatalogRepository`` to be passed in. Default for
  ``doyoutrade-cli stock lookup`` because the CLI already builds the
  runtime.
- ``akshare_a`` — query the upstream akshare A-share listing tables
  (``stock_info_a_code_name`` + ``stock_info_bj_name_code``). Pays a
  network round-trip on cache miss; akshare prints a tqdm progress bar
  for the Beijing leg, which is why this is no longer the default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from doyoutrade.data.instrument_universe.akshare_a import search_akshare_a
from doyoutrade.data.instrument_universe.local_catalog import search_local_catalog

if TYPE_CHECKING:
    from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository

InstrumentSearchItem = Dict[str, str]

ALLOWED_INSTRUMENT_SOURCES: frozenset[str] = frozenset({"local_catalog", "akshare_a"})


async def search_instrument_universe(
    *,
    source: str,
    q: str,
    limit: int,
    instrument_catalog_repository: "SqlAlchemyInstrumentCatalogRepository | None" = None,
) -> Dict[str, Any]:
    src = (source or "").strip()
    if src not in ALLOWED_INSTRUMENT_SOURCES:
        allowed = ", ".join(sorted(ALLOWED_INSTRUMENT_SOURCES))
        raise ValueError(f"unknown or disallowed source={source!r}; allowed: {allowed}")

    items: List[InstrumentSearchItem]
    if src == "akshare_a":
        items = await search_akshare_a(q=q, limit=limit)
    elif src == "local_catalog":
        if instrument_catalog_repository is None:
            raise ValueError(
                "source='local_catalog' requires instrument_catalog_repository; "
                "the in-process caller did not provide one. "
                "Fall back to source='akshare_a' or wire the runtime's "
                "instrument_catalog_repository into the caller."
            )
        items = await search_local_catalog(
            repository=instrument_catalog_repository, q=q, limit=limit
        )
    else:  # pragma: no cover - guarded by ALLOWED_INSTRUMENT_SOURCES above
        raise ValueError(f"unhandled source={source!r}")

    return {"source": src, "items": items}
