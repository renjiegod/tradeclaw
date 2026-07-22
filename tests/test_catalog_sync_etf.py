"""ETF classification through the catalog sync paths (akshare + QMT).

Locks the two changes that let ETFs reach the tradable catalog:

* akshare sync threads each listing row's ``instrument_type`` ("stock"/"etf")
  and writes both tradable.
* QMT sync no longer filters ETFs out of sector lists, and pins ``etf`` by the
  canonical-symbol classifier regardless of the (inconsistent) QMT type string.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncEngine

from doyoutrade.data.instrument_catalog.sync_akshare import sync_akshare_catalog
from doyoutrade.data.instrument_catalog.sync_qmt import sync_qmt_catalog
from doyoutrade.persistence.db import (
    Base,
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository


class _Sector:
    def __init__(self, name: str) -> None:
        self.sector_name = name


class _SectorStocks:
    def __init__(self, codes: list[str]) -> None:
        self.stock_list = codes


class _InstrumentInfo:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return dict(self._payload)


class _FakeQmtData:
    """Minimal stand-in for ``rest_client._client.data`` used by sync_qmt."""

    def __init__(self, codes: list[str], info_by_symbol: dict[str, dict]) -> None:
        self._codes = codes
        self._info = info_by_symbol

    async def get_sector_list(self) -> list[_Sector]:
        return [_Sector("沪深A股")]

    async def get_stock_list_in_sector(self, name: str) -> _SectorStocks:
        return _SectorStocks(self._codes)

    async def get_instrument_info(self, sym: str) -> _InstrumentInfo:
        return _InstrumentInfo(self._info.get(sym, {"IsTrading": True}))


class _FakeRestClient:
    def __init__(self, data: _FakeQmtData) -> None:
        class _Inner:
            pass

        self._client = _Inner()
        self._client.data = data


class CatalogSyncEtfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "catalog.db"
        self.engine: AsyncEngine
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyInstrumentCatalogRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def _row(self, symbol: str) -> dict:
        item = await self.repo.get(symbol)
        self.assertIsNotNone(item, symbol)
        return item

    async def test_akshare_sync_tags_etf_rows_tradable(self) -> None:
        listing_rows = [
            {"symbol": "600000.SH", "name": "浦发银行", "market": "CN", "instrument_type": "stock"},
            {"symbol": "510300.SH", "name": "沪深300ETF", "market": "CN", "instrument_type": "etf"},
        ]
        with patch(
            "doyoutrade.data.instrument_catalog.sync_akshare._sync_fetch_spot_rows",
            return_value=listing_rows,
        ):
            result = await sync_akshare_catalog(self.repo, mode="symbols", symbols=["600000.SH", "510300.SH"])
        self.assertGreaterEqual(result["rows_seen"], 2)

        stock = await self._row("600000.SH")
        etf = await self._row("510300.SH")
        self.assertEqual(stock["instrument_type"], "stock")
        self.assertEqual(etf["instrument_type"], "etf")
        # Both on-exchange → tradable so they can enter strategy/backtest universe.
        self.assertTrue(etf["is_tradable"])

    async def test_qmt_sync_keeps_etf_and_pins_type(self) -> None:
        # QMT sector list mixes a stock, an ETF, and a convertible bond.
        codes = ["600000.SH", "510300.SH", "110000.SH"]
        info = {
            "600000.SH": {"IsTrading": True, "instrument_type": "stock", "instrument_name": "浦发银行"},
            # QMT often returns a non-"etf" type string for funds; sync must
            # still pin "etf" from the canonical-symbol classifier.
            "510300.SH": {"IsTrading": True, "instrument_type": "fund", "instrument_name": "沪深300ETF"},
        }
        rest = _FakeRestClient(_FakeQmtData(codes, info))
        await sync_qmt_catalog(self.repo, rest, mode="full")

        stock = await self._row("600000.SH")
        etf = await self._row("510300.SH")
        self.assertEqual(stock["instrument_type"], "stock")
        self.assertEqual(etf["instrument_type"], "etf")
        self.assertTrue(etf["is_tradable"])
        # The convertible bond stays filtered out of the catalog entirely.
        self.assertIsNone(await self.repo.get("110000.SH"))


if __name__ == "__main__":
    unittest.main()
