import tempfile
import unittest
from pathlib import Path

from doyoutrade.config import get_config
from sqlalchemy.ext.asyncio import AsyncEngine

from doyoutrade.data.instrument_catalog.a_share_equity import (
    is_cn_a_share_equity_symbol,
    is_cn_a_share_etf_symbol,
    is_cn_a_share_index_symbol,
)
from doyoutrade.data.instrument_catalog.normalize import (
    canonical_symbol_from_qmt_stock_code,
    canonical_symbol_from_doyoutrade_or_akshare,
)
from doyoutrade.data.instrument_catalog.validation import (
    CatalogNotTradableError,
    CatalogValidationError,
    ensure_symbols_in_catalog,
)
from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.repositories import SqlAlchemyInstrumentCatalogRepository
from doyoutrade.platform.service import TradingPlatformService
from doyoutrade.runtime.scheduler import RuntimeScheduler


class NormalizeTests(unittest.TestCase):
    def test_ak_a_share_suffix(self):
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("600000"), "600000.SH")
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("000001"), "000001.SZ")

    def test_qmt_suffixed(self):
        self.assertEqual(canonical_symbol_from_qmt_stock_code("600000.SH"), "600000.SH")

    def test_qmt_wrong_exchange_suffix_collapses_to_one(self):
        """QMT may emit both .SH and .SZ for the same 6-digit code; canonical is akshare rules."""
        self.assertEqual(
            canonical_symbol_from_qmt_stock_code("000036.SH"),
            canonical_symbol_from_qmt_stock_code("000036.SZ"),
        )
        self.assertEqual(canonical_symbol_from_qmt_stock_code("000036.SH"), "000036.SZ")

    def test_sh_etf_codes_get_sh_suffix(self):
        """上交所 ETF (51/56/58xxxx) 必须落到 .SH，不能被兜底成 .SZ。"""
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("510300"), "510300.SH")
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("588000"), "588000.SH")
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("560010"), "560010.SH")
        # 深交所 ETF (15xxxx) 仍是 .SZ
        self.assertEqual(canonical_symbol_from_doyoutrade_or_akshare("159915"), "159915.SZ")


class EtfFilterTests(unittest.TestCase):
    def test_recognises_sh_and_sz_etf(self):
        self.assertTrue(is_cn_a_share_etf_symbol("510300.SH"))
        self.assertTrue(is_cn_a_share_etf_symbol("588000.SH"))
        self.assertTrue(is_cn_a_share_etf_symbol("560010.SH"))
        self.assertTrue(is_cn_a_share_etf_symbol("159915.SZ"))

    def test_rejects_stocks_and_non_etf_funds(self):
        # 普通股票不是 ETF
        self.assertFalse(is_cn_a_share_etf_symbol("600000.SH"))
        self.assertFalse(is_cn_a_share_etf_symbol("000001.SZ"))
        self.assertFalse(is_cn_a_share_etf_symbol("300750.SZ"))
        # LOF (16xxxx SZ) / 封闭式 (18xxxx SZ) / 债券可转债 —— 有意排除
        self.assertFalse(is_cn_a_share_etf_symbol("161725.SZ"))
        self.assertFalse(is_cn_a_share_etf_symbol("184801.SZ"))
        self.assertFalse(is_cn_a_share_etf_symbol("115940.SZ"))
        # 非规范形式
        self.assertFalse(is_cn_a_share_etf_symbol("510300"))
        self.assertFalse(is_cn_a_share_etf_symbol("00700.HK"))

    def test_etf_and_equity_are_disjoint(self):
        """同一 symbol 不会既是股票又是 ETF。"""
        for sym in ("600000.SH", "510300.SH", "159915.SZ", "000001.SZ"):
            self.assertFalse(
                is_cn_a_share_equity_symbol(sym) and is_cn_a_share_etf_symbol(sym),
                sym,
            )


class IndexFilterTests(unittest.TestCase):
    def test_recognises_sh_and_sz_index(self):
        self.assertTrue(is_cn_a_share_index_symbol("000001.SH"))  # 上证综指
        self.assertTrue(is_cn_a_share_index_symbol("000300.SH"))  # 沪深300
        self.assertTrue(is_cn_a_share_index_symbol("000905.SH"))  # 中证500
        self.assertTrue(is_cn_a_share_index_symbol("399001.SZ"))  # 深证成指
        self.assertTrue(is_cn_a_share_index_symbol("399006.SZ"))  # 创业板指

    def test_rejects_stocks_etf_and_malformed(self):
        # 同数字不同市场：000001.SZ 是平安银行（个股），不是指数
        self.assertFalse(is_cn_a_share_index_symbol("000001.SZ"))
        self.assertFalse(is_cn_a_share_index_symbol("600000.SH"))
        self.assertFalse(is_cn_a_share_index_symbol("300750.SZ"))
        self.assertFalse(is_cn_a_share_index_symbol("510300.SH"))  # ETF
        # 指数与 ETF 分类互斥
        self.assertFalse(is_cn_a_share_etf_symbol("000001.SH"))
        # 非规范形式
        self.assertFalse(is_cn_a_share_index_symbol("000001"))
        self.assertFalse(is_cn_a_share_index_symbol("00700.HK"))


class ASshareFilterTests(unittest.TestCase):
    def test_allows_main_board_and_chinext(self):
        self.assertTrue(is_cn_a_share_equity_symbol("600000.SH"))
        self.assertTrue(is_cn_a_share_equity_symbol("000001.SZ"))
        self.assertTrue(is_cn_a_share_equity_symbol("300750.SZ"))
        self.assertTrue(is_cn_a_share_equity_symbol("688001.SH"))

    def test_rejects_etf_fund_bond_style_codes(self):
        # Log samples that returned 400 on instrument info
        self.assertFalse(is_cn_a_share_equity_symbol("235637.SZ"))
        self.assertFalse(is_cn_a_share_equity_symbol("115940.SZ"))
        self.assertFalse(is_cn_a_share_equity_symbol("186247.SZ"))
        self.assertFalse(is_cn_a_share_equity_symbol("510300.SH"))


class CatalogRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "catalog.db"
        self.engine: AsyncEngine
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyInstrumentCatalogRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_find_missing_and_upsert(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                }
            ]
        )
        missing = await self.repo.find_missing_symbols(["600000.SH", "000001.SZ"])
        self.assertEqual(missing, ["000001.SZ"])
        page, total = await self.repo.list_page(q="浦发", limit=10, offset=0)
        self.assertEqual(total, 1)
        self.assertEqual(page[0]["symbol"], "600000.SH")

    async def test_find_non_tradable_returns_only_explicit_false(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
                {
                    "symbol": "000001.SH",
                    "display_name": "上证指数",
                    "market": "CN",
                    "instrument_type": "index",
                    "is_tradable": False,
                    "last_sync_source": "index_seed",
                    "last_sync_at": now,
                    "raw": None,
                },
                # NULL is_tradable (unknown) must NOT be reported as non-tradable.
                {
                    "symbol": "688001.SH",
                    "display_name": "华兴源创",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": None,
                    "last_sync_source": "qmt",
                    "last_sync_at": now,
                    "raw": None,
                },
            ]
        )
        non_tradable = await self.repo.find_non_tradable_symbols(
            ["600000.SH", "000001.SH", "688001.SH", "NOPE.SZ"]
        )
        # Only the explicit index seed; absent symbol and NULL are excluded.
        self.assertEqual(non_tradable, ["000001.SH"])

    async def test_list_page_matches_pinyin_and_initials(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
                {
                    "symbol": "600519.SH",
                    "display_name": "贵州茅台",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
            ]
        )

        by_pinyin, total_py = await self.repo.list_page(q="pufa", limit=10, offset=0)
        self.assertEqual(total_py, 1)
        self.assertEqual(by_pinyin[0]["symbol"], "600000.SH")

        by_initials, total_init = await self.repo.list_page(q="pfyh", limit=10, offset=0)
        self.assertEqual(total_init, 1)
        self.assertEqual(by_initials[0]["symbol"], "600000.SH")

        by_mt, total_mt = await self.repo.list_page(q="mt", limit=10, offset=0)
        self.assertEqual(total_mt, 1)
        self.assertEqual(by_mt[0]["symbol"], "600519.SH")

    async def test_list_page_tradable_only_filters_seeded_indices(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
                {
                    "symbol": "000001.SH",
                    "display_name": "上证指数",
                    "market": "CN",
                    "instrument_type": "index",
                    "is_tradable": False,
                    "last_sync_source": "index_seed",
                    "last_sync_at": now,
                    "raw": None,
                },
            ]
        )

        page, total = await self.repo.list_page(
            q=None,
            limit=10,
            offset=0,
            tradable_only=True,
        )
        self.assertEqual(total, 1)
        self.assertEqual([row["symbol"] for row in page], ["600000.SH"])

    async def test_delete_symbols_and_delete_all(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
                {
                    "symbol": "000001.SZ",
                    "display_name": "平安银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
            ]
        )
        n = await self.repo.delete_symbols(["600000.SH", "600000.SH"])
        self.assertEqual(n, 1)
        _, total = await self.repo.list_page(q=None, limit=50, offset=0)
        self.assertEqual(total, 1)
        n2 = await self.repo.delete_all()
        self.assertEqual(n2, 1)
        _, total2 = await self.repo.list_page(q=None, limit=50, offset=0)
        self.assertEqual(total2, 0)


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "catalog.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyInstrumentCatalogRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_ensure_raises_catalog_validation_error(self):
        with self.assertRaises(CatalogValidationError) as ctx:
            await ensure_symbols_in_catalog(self.repo, ["NOPE.SH"])
        self.assertEqual(ctx.exception.missing_symbols, ["NOPE.SH"])

    async def _seed_stock_and_index(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await self.repo.upsert_rows(
            [
                {
                    "symbol": "600000.SH",
                    "display_name": "浦发银行",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": now,
                    "raw": None,
                },
                {
                    "symbol": "000001.SH",
                    "display_name": "上证指数",
                    "market": "CN",
                    "instrument_type": "index",
                    "is_tradable": False,
                    "last_sync_source": "index_seed",
                    "last_sync_at": now,
                    "raw": None,
                },
            ]
        )

    async def test_ensure_tradable_only_rejects_non_tradable_symbol(self):
        await self._seed_stock_and_index()
        with self.assertRaises(CatalogNotTradableError) as ctx:
            await ensure_symbols_in_catalog(
                self.repo, ["600000.SH", "000001.SH"], tradable_only=True
            )
        # Only the index is flagged; the stock passes the tradable check.
        self.assertEqual(ctx.exception.non_tradable_symbols, ["000001.SH"])
        # Distinct failure mode from "missing": it subclasses CatalogError but
        # not CatalogValidationError.
        self.assertNotIsInstance(ctx.exception, CatalogValidationError)

    async def test_ensure_tradable_only_allows_tradable_symbols(self):
        await self._seed_stock_and_index()
        # No raise when the universe is fully tradable.
        await ensure_symbols_in_catalog(self.repo, ["600000.SH"], tradable_only=True)

    async def test_ensure_tradable_only_missing_takes_precedence(self):
        # A missing symbol is reported as CatalogValidationError before the
        # tradable-only check runs (non-tradable requires catalog presence).
        await self._seed_stock_and_index()
        with self.assertRaises(CatalogValidationError):
            await ensure_symbols_in_catalog(
                self.repo, ["NOPE.SH", "000001.SH"], tradable_only=True
            )


class _MemoryCatalogRepository:
    def __init__(self, rows: list[dict]):
        self._rows = {str(row["symbol"]).strip().upper(): dict(row) for row in rows}

    async def get(self, symbol: str) -> dict | None:
        return self._rows.get(str(symbol or "").strip().upper())


class ServiceCatalogLookupTests(unittest.IsolatedAsyncioTestCase):
    def _build_service(self, rows: list[dict]) -> TradingPlatformService:
        return TradingPlatformService(
            scheduler=RuntimeScheduler(),
            app_cfg=get_config(),
            worker_factory=lambda config, ms, acct=None: None,
            task_repository=object(),
            instrument_catalog_repository=_MemoryCatalogRepository(rows),
        )

    async def test_get_item_falls_back_to_canonical_a_share_symbol(self):
        service = self._build_service(
            [
                {
                    "symbol": "000036.SZ",
                    "display_name": "华联控股",
                    "market": "CN",
                    "instrument_type": "stock",
                    "is_tradable": True,
                    "last_sync_source": "akshare",
                    "last_sync_at": None,
                    "raw": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        )

        row = await service.get_instrument_catalog_item("000036.SH")
        assert row is not None
        self.assertEqual(row["symbol"], "000036.SZ")
        self.assertEqual(row["display_name"], "华联控股")

    async def test_get_item_returns_built_in_index_seed_when_catalog_is_empty(self):
        service = self._build_service([])

        row = await service.get_instrument_catalog_item("000001.SH")
        assert row is not None
        self.assertEqual(row["symbol"], "000001.SH")
        self.assertEqual(row["display_name"], "上证指数")
        self.assertEqual(row["instrument_type"], "index")
        self.assertFalse(row["is_tradable"])


if __name__ == "__main__":
    unittest.main()
