"""Upsert instrument_catalog rows using qmt-proxy Data API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient

from doyoutrade.data.instrument_catalog.a_share_equity import is_cn_a_share_equity_symbol
from doyoutrade.data.instrument_catalog.index_seeds import index_seed_rows
from doyoutrade.data.instrument_catalog.normalize import canonical_symbol_from_qmt_stock_code

from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository

_QMT_INSTRUMENT_INFO_CONCURRENCY = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _enrich_rows_from_instrument_info(
    data: Any,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fill ``display_name`` / ``raw`` from ``get_instrument_info`` (full QMT sync)."""

    sem = asyncio.Semaphore(_QMT_INSTRUMENT_INFO_CONCURRENCY)

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        sym = out["symbol"]
        async with sem:
            try:
                info = await data.get_instrument_info(sym)
            except Exception as exc:
                out["raw"] = {
                    **(out.get("raw") or {}),
                    "instrument_info_error": str(exc)[:500],
                }
                out["display_name"] = sym
                return out
            payload = info.model_dump() if hasattr(info, "model_dump") else {}
            display = (
                payload.get("instrument_name")
                or payload.get("InstrumentName")
                or payload.get("InstrumentID")
            )
            if display is not None and str(display).strip():
                out["display_name"] = str(display).strip()
            else:
                out["display_name"] = sym
            out["is_tradable"] = payload.get("IsTrading")
            it = str(payload.get("instrument_type") or "stock")[:64]
            out["instrument_type"] = it
            out["raw"] = {**(out.get("raw") or {}), "instrument_info": payload}
            return out

    return await asyncio.gather(*[one(dict(r)) for r in rows])


async def sync_qmt_catalog(
    repo: SqlAlchemyInstrumentCatalogRepository,
    rest_client: QmtProxyRestClient,
    *,
    mode: str,
    symbols: list[str] | None = None,
) -> dict[str, int]:
    data = rest_client._client.data
    if mode == "full":
        sectors = await data.get_sector_list()
        seen: set[str] = set()
        out_rows: list[dict[str, Any]] = []
        for sec in sectors:
            sector_name = str(sec.sector_name).strip()
            if not sector_name:
                continue
            try:
                resp = await data.get_stock_list_in_sector(sector_name)
            except Exception:
                continue
            raw_list = getattr(resp, "stock_list", None) or []
            for code in raw_list:
                sym = canonical_symbol_from_qmt_stock_code(str(code))
                if not sym or sym in seen:
                    continue
                if not is_cn_a_share_equity_symbol(sym):
                    continue
                seen.add(sym)
                out_rows.append(
                    {
                        "symbol": sym,
                        "display_name": None,
                        "market": "CN",
                        "instrument_type": "stock",
                        "is_tradable": None,
                        "last_sync_source": "qmt",
                        "last_sync_at": _utcnow(),
                        "raw": {"source": "qmt", "sector": sector_name, "stock_code": str(code)},
                    }
                )
        out_rows = await _enrich_rows_from_instrument_info(data, out_rows)
        # 指数被 is_cn_a_share_equity_symbol 过滤掉了，用内置种子补齐。放在 enrich
        # 之后追加，避免对指数调 get_instrument_info（可能报错 / 覆盖 instrument_type）。
        out_rows.extend(index_seed_rows(source="qmt"))
        inserted, updated = await repo.upsert_rows(out_rows)
        return {"inserted": inserted, "updated": updated, "rows_seen": len(out_rows)}

    if mode != "symbols":
        raise ValueError(f"sync_qmt_catalog: invalid mode {mode!r}")

    want_in = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    want = [canonical_symbol_from_qmt_stock_code(s) for s in want_in]
    want = list(dict.fromkeys(want))
    if not want:
        return {"inserted": 0, "updated": 0, "rows_seen": 0}

    out_rows = []
    for sym in want:
        info = await data.get_instrument_info(sym)
        payload = info.model_dump() if hasattr(info, "model_dump") else {}
        display = (
            payload.get("instrument_name")
            or payload.get("InstrumentName")
            or payload.get("InstrumentID")
            or sym
        )
        itype = str(payload.get("instrument_type") or "stock")[:64]
        out_rows.append(
            {
                "symbol": sym,
                "display_name": str(display) if display is not None else sym,
                "market": "CN",
                "instrument_type": itype,
                "is_tradable": payload.get("IsTrading"),
                "last_sync_source": "qmt",
                "last_sync_at": _utcnow(),
                "raw": payload,
            }
        )
    inserted, updated = await repo.upsert_rows(out_rows)
    return {"inserted": inserted, "updated": updated, "rows_seen": len(out_rows)}
