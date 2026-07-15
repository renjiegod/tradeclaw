"""qmt-proxy-backed sector / industry / concept membership provider.

Wraps the qmt-proxy ``/api/v1/data/sectors`` and ``/api/v1/data/sector``
endpoints (already exposed by :class:`doyoutrade.infra.qmt_proxy_client.
QmtProxyRestClient`) into the :class:`doyoutrade.data.protocols.
SectorProvider` contract.

QMT sector lists may attach a wrong exchange suffix to a constituent code,
so codes are re-derived through ``canonical_symbol_from_qmt_stock_code``
(the same normalization the OHLCV listing path uses) before they reach a
universe file.

Failure-mode discipline (per CLAUDE.md §错误可见性): a transport failure
propagates so the ``data_sector`` tool surfaces ``sector_fetch_failed``;
an empty board returns ``[]`` (mapped to ``sector_empty``). The
``data_provider.<method>`` debug event always fires.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from doyoutrade.core.models import SectorMember
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.normalize import (
    canonical_symbol_from_qmt_stock_code,
)
from doyoutrade.data.protocols import PROVIDER_NAME_QMT, ProviderCapabilities

logger = logging.getLogger(__name__)


class QmtSectorProvider:
    """Sector membership source backed by qmt-proxy board endpoints."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_QMT,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        # qmt-proxy needs a configured base_url / session — the factory
        # skips this provider during ``auto`` selection when unconfigured.
        requires_auth=True,
        is_realtime_capable=True,
        max_history_years=None,
    )

    def __init__(self, client):
        self.client = client

    async def list_sectors(self, *, sector_type: str | None = None) -> List[str]:
        rows = await self.client.fetch_sectors()
        names: List[str] = []
        for row in rows:
            if sector_type and (row.get("sector_type") or "") != sector_type:
                continue
            name = str(row.get("sector_name") or "").strip()
            if name:
                names.append(name)
        names = list(dict.fromkeys(names))
        _emit_event("data_provider.list_sectors", {"sector_type": sector_type, "count": len(names)})
        return names

    async def get_sector_members(
        self, sector_name: str, *, sector_type: str | None = None
    ) -> List[SectorMember]:
        row = await self.client.fetch_sector_members(sector_name, sector_type)
        resolved_type = str(row.get("sector_type") or sector_type or "")
        members: List[SectorMember] = []
        for raw_code in row.get("stock_list") or []:
            code = canonical_symbol_from_qmt_stock_code(str(raw_code))
            if not code:
                continue
            members.append(
                SectorMember(
                    sector_name=sector_name,
                    code=code,
                    # qmt-proxy's sector endpoint returns codes only (no
                    # names); leave name empty rather than fabricating it.
                    name="",
                    provider=PROVIDER_NAME_QMT,
                    sector_type=resolved_type,
                )
            )
        _emit_event(
            "data_provider.get_sector_members",
            {"sector_name": sector_name, "sector_type": resolved_type, "count": len(members)},
        )
        return members


def _emit_event(event_name: str, payload: dict) -> None:
    payload = {"provider": PROVIDER_NAME_QMT, **payload}
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


__all__ = ["QmtSectorProvider"]
