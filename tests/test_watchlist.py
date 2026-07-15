"""Watchlist (自选股) feature tests: persistence, universe-by-tag resolution,
the ``ctx.dp.watchlist_symbols`` snapshot, K-line sync scoping, and the
/watchlist + /market/quotes API surface.

Covers the run_id-independent pieces of the watchlist feature; the end-to-end
``@watchlist:<tag>`` → cycle throughput is exercised in the e2e suite.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import StateConflictError
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import SqlAlchemyWatchlistRepository


# --------------------------------------------------------------------------- #
# 1. Repository CRUD + tag filter + snapshot + tags + duplicate
# --------------------------------------------------------------------------- #
class WatchlistRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "watchlist.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyWatchlistRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_crud_tag_filter_snapshot_and_tags(self) -> None:
        self.assertEqual(await self.repo.list_entries(), [])
        a = await self.repo.upsert_entry(
            {"symbol": "600000.SH", "display_name": "浦发银行", "tags": ["核心池", "银行"]}
        )
        self.assertTrue(a["id"].startswith("wl-"))
        self.assertEqual(a["tags"], ["核心池", "银行"])
        b = await self.repo.upsert_entry(
            {"symbol": "000001.SZ", "tags": ["核心池"], "note": "关注"}
        )

        # list_entries + tag filter
        self.assertEqual(
            {e["symbol"] for e in await self.repo.list_entries()},
            {"600000.SH", "000001.SZ"},
        )
        self.assertEqual(
            {e["symbol"] for e in await self.repo.list_entries(tag="银行")},
            {"600000.SH"},
        )
        self.assertEqual(
            {e["symbol"] for e in await self.repo.list_entries(tag="核心池")},
            {"600000.SH", "000001.SZ"},
        )

        # list_symbols + snapshot
        self.assertEqual(set(await self.repo.list_symbols()), {"600000.SH", "000001.SZ"})
        self.assertEqual(await self.repo.list_symbols(tag="银行"), ["600000.SH"])
        snap = await self.repo.snapshot()
        self.assertEqual(snap["600000.SH"], ["核心池", "银行"])
        self.assertEqual(snap["000001.SZ"], ["核心池"])

        # list_tags counts
        tags = {row["tag"]: row["count"] for row in await self.repo.list_tags()}
        self.assertEqual(tags, {"核心池": 2, "银行": 1})

        # patch update keeps untouched fields; tags only replaced when sent
        updated = await self.repo.upsert_entry({"id": a["id"], "note": "盯紧"})
        self.assertEqual(updated["note"], "盯紧")
        self.assertEqual(updated["tags"], ["核心池", "银行"])  # not clobbered

        # delete
        await self.repo.delete_entry(b["id"])
        self.assertIsNone(await self.repo.get_entry(b["id"]))
        self.assertEqual(await self.repo.list_symbols(), ["600000.SH"])

    async def test_duplicate_symbol_raises_state_conflict(self) -> None:
        await self.repo.upsert_entry({"symbol": "600000.SH"})
        with self.assertRaises(StateConflictError) as ctx:
            await self.repo.upsert_entry({"symbol": "600000.SH"})
        self.assertIn("duplicate_watchlist_symbol", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 2. universe token resolution (@watchlist:<tag>)
# --------------------------------------------------------------------------- #
class _FakeWatchlistRepo:
    def __init__(self, symbols_by_tag: dict[str | None, list[str]]):
        self._by_tag = symbols_by_tag

    async def list_symbols(self, tag: str | None = None) -> list[str]:
        return list(self._by_tag.get(tag, []))

    async def snapshot(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for tag, syms in self._by_tag.items():
            if tag is None:
                continue
            for s in syms:
                out.setdefault(s, []).append(tag)
        return out


class WatchlistUniverseTests(unittest.IsolatedAsyncioTestCase):
    def test_split_universe_tokens(self) -> None:
        from doyoutrade.runtime.watchlist_universe import split_universe_tokens

        plain, tags = split_universe_tokens(
            ["600000.SH", "@watchlist:核心池", "@watchlist:*"]
        )
        self.assertEqual(plain, ["600000.SH"])
        self.assertEqual(tags, ["核心池", "*"])

    async def test_resolve_expands_dedupes_and_passes_through(self) -> None:
        from doyoutrade.runtime.watchlist_universe import resolve_watchlist_universe

        repo = _FakeWatchlistRepo(
            {"核心池": ["600000.SH", "000001.SZ"], None: ["600000.SH", "300750.SZ"]}
        )
        # token + plain symbol, dedup preserves order
        resolved = await resolve_watchlist_universe(
            ["000001.SZ", "@watchlist:核心池"], repo
        )
        self.assertEqual(resolved, ["000001.SZ", "600000.SH"])
        # @watchlist:* expands to list_symbols(None)
        resolved_all = await resolve_watchlist_universe(["@watchlist:*"], repo)
        self.assertEqual(set(resolved_all), {"600000.SH", "300750.SZ"})
        # no tokens → unchanged, no repo call needed
        passthrough = await resolve_watchlist_universe(["600000.SH"], repo)
        self.assertEqual(passthrough, ["600000.SH"])


# --------------------------------------------------------------------------- #
# 3. WatchlistSnapshot + ctx.dp.watchlist_symbols
# --------------------------------------------------------------------------- #
class WatchlistSnapshotDpTests(unittest.TestCase):
    def _dp(self, snapshot):
        from doyoutrade.strategy_sdk.data_provider import DataProvider

        return DataProvider(
            current_symbol="600000.SH",
            now=datetime(2026, 6, 7, tzinfo=timezone.utc),
            is_backtest=True,
            _watchlist_snapshot=snapshot,
        )

    def test_watchlist_symbols_filters_by_tag(self) -> None:
        from doyoutrade.strategy_sdk.watchlist_snapshot import WatchlistSnapshot

        snap = WatchlistSnapshot.from_mapping(
            {"600000.SH": ["核心池", "银行"], "000001.SZ": ["核心池"]}
        )
        dp = self._dp(snap)
        self.assertEqual(set(dp.watchlist_symbols()), {"600000.SH", "000001.SZ"})
        self.assertEqual(dp.watchlist_symbols(tag="银行"), ["600000.SH"])
        self.assertEqual(set(dp.watchlist_symbols(tag="核心池")), {"600000.SH", "000001.SZ"})

    def test_missing_snapshot_raises_visible_error(self) -> None:
        from doyoutrade.strategy_sdk.errors import DataAccessError

        dp = self._dp(None)
        with self.assertRaises(DataAccessError) as ctx:
            dp.watchlist_symbols()
        self.assertEqual(getattr(ctx.exception, "error_code", None), "invalid_argument")


# --------------------------------------------------------------------------- #
# 4. K-line sync scoping to the watchlist
# --------------------------------------------------------------------------- #
class _FakeCatalog:
    def __init__(self, rows):
        self.rows = rows

    async def list_page(self, *, q, limit, offset):
        return self.rows[offset : offset + limit], len(self.rows)


class MarketSyncWatchlistScopeTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, watchlist_repo):
        from doyoutrade.data.market_sync import MarketDataSyncService

        catalog = _FakeCatalog(
            [
                {"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "000001.SZ", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "300750.SZ", "instrument_type": "stock", "is_tradable": True},
            ]
        )
        return MarketDataSyncService(
            market_repository=object(),
            instrument_catalog_repository=catalog,
            provider_factory=lambda: object(),
            intervals=("1d",),
            lookback_years=1,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
            watchlist_repository=watchlist_repo,
        )

    async def test_scopes_to_watchlist_symbols(self) -> None:
        repo = _FakeWatchlistRepo({None: ["600000.SH", "300750.SZ"]})
        service = self._service(repo)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ):
            symbols = await service._list_symbols()
        self.assertEqual({s for s, _ in symbols}, {"600000.SH", "300750.SZ"})

    async def test_empty_watchlist_syncs_nothing(self) -> None:
        repo = _FakeWatchlistRepo({None: []})
        service = self._service(repo)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ):
            symbols = await service._list_symbols()
        self.assertEqual(symbols, [])

    async def test_no_repo_keeps_full_catalog(self) -> None:
        service = self._service(None)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ):
            symbols = await service._list_symbols()
        self.assertEqual(
            {s for s, _ in symbols}, {"600000.SH", "000001.SZ", "300750.SZ"}
        )


# --------------------------------------------------------------------------- #
# 5. /watchlist + /market/quotes API surface
# --------------------------------------------------------------------------- #
class _WatchlistFakeService:
    """Minimal service exposing the watchlist methods the routes call, plus the
    attributes create_app inspects."""

    def __init__(self):
        self._entries: dict[str, dict] = {}
        self._seq = 0
        # attributes create_app / AssistantService construction reach for
        self.strategy_runtime = None
        self.cycle_run_repository = None

    async def list_watchlist(self, tag=None):
        out = list(self._entries.values())
        if tag:
            out = [e for e in out if tag in e["tags"]]
        return out

    async def get_watchlist_entry(self, entry_id):
        rec = self._entries.get(entry_id)
        if rec is None:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        return rec

    async def add_watchlist_entry(self, payload):
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        if any(e["symbol"] == symbol for e in self._entries.values()):
            raise StateConflictError(f"duplicate_watchlist_symbol: {symbol}")
        self._seq += 1
        entry_id = f"wl-{self._seq:012d}"
        rec = {
            "id": entry_id,
            "symbol": symbol,
            "display_name": payload.get("display_name"),
            "tags": list(payload.get("tags") or []),
            "note": payload.get("note") or "",
            "sort_order": int(payload.get("sort_order") or 0),
            "created_at": None,
            "updated_at": None,
        }
        self._entries[entry_id] = rec
        return rec

    async def update_watchlist_entry(self, entry_id, payload):
        rec = self._entries.get(entry_id)
        if rec is None:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        if "tags" in payload:
            rec["tags"] = list(payload.get("tags") or [])
        if "note" in payload:
            rec["note"] = payload.get("note") or ""
        return rec

    async def delete_watchlist_entry(self, entry_id):
        if entry_id not in self._entries:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        del self._entries[entry_id]

    async def list_watchlist_tags(self):
        counts: dict[str, int] = {}
        for e in self._entries.values():
            for t in e["tags"]:
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "count": c} for t, c in counts.items()]


def _build_app(quote_stream_service=None):
    from doyoutrade.api.app import create_app
    from tests.test_api_app import _FakeApprovalGate, _FakeAssistantService

    return create_app(
        _WatchlistFakeService(),
        _FakeApprovalGate(),
        assistant_service=_FakeAssistantService(),
        quote_stream_service=quote_stream_service,
    )


class WatchlistApiTests(unittest.TestCase):
    def test_watchlist_crud_tags_and_conflict(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            self.assertEqual(client.get("/watchlist").json(), {"items": []})

            created = client.post(
                "/watchlist",
                json={"symbol": "600000.SH", "tags": ["核心池"], "note": "关注"},
            )
            self.assertEqual(created.status_code, 201)
            wid = created.json()["id"]
            self.assertTrue(wid.startswith("wl-"))

            # duplicate symbol → 409
            dup = client.post("/watchlist", json={"symbol": "600000.SH"})
            self.assertEqual(dup.status_code, 409)

            # get + 404
            self.assertEqual(client.get(f"/watchlist/{wid}").status_code, 200)
            self.assertEqual(client.get("/watchlist/wl-nope").status_code, 404)

            # tags endpoint
            self.assertEqual(
                client.get("/watchlist/tags").json(),
                {"items": [{"tag": "核心池", "count": 1}]},
            )

            # tag filter
            self.assertEqual(len(client.get("/watchlist?tag=核心池").json()["items"]), 1)
            self.assertEqual(len(client.get("/watchlist?tag=missing").json()["items"]), 0)

            # update (patch tags)
            up = client.put(f"/watchlist/{wid}", json={"tags": ["核心池", "银行"]})
            self.assertEqual(set(up.json()["tags"]), {"核心池", "银行"})

            # delete → 204 then 404
            self.assertEqual(client.delete(f"/watchlist/{wid}").status_code, 204)
            self.assertEqual(client.get(f"/watchlist/{wid}").status_code, 404)

    def test_market_quotes_disconnected_when_no_stream_service(self) -> None:
        app = _build_app(quote_stream_service=None)
        with TestClient(app) as client:
            resp = client.get("/market/quotes", params={"symbol": ["600000.SH", "000001.SZ"]})
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertEqual(len(items), 2)
            self.assertTrue(all(i["status"] == "qmt_disconnected" for i in items))
            self.assertTrue(all(i["price"] is None for i in items))
            # empty symbol list → empty items, not an error
            self.assertEqual(client.get("/market/quotes").json(), {"items": []})


if __name__ == "__main__":
    unittest.main()
