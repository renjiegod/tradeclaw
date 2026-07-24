"""Auto chain must drop providers that cannot serve the requested interval/symbol.

``data run --interval 60m`` on an index (000001.SH) used to keep baostock at the
head of the auto chain; capability skip then relied on runtime alone. Filtering
at chain-resolution time keeps unsupported sources out of the stack entirely
for every interval, not just the ones with a pre-existing carve-out.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from doyoutrade.config import DataSettings
from doyoutrade.data.factory import (
    PROVIDER_AKSHARE,
    PROVIDER_BAOSTOCK,
    PROVIDER_MOOTDX,
    _resolve_auto_chain,
)


def _data_settings() -> DataSettings:
    return DataSettings(default_provider="auto")


class AutoChainIntervalFilterTests(unittest.TestCase):
    def test_index_60m_drops_baostock_from_auto_chain(self) -> None:
        chain = _resolve_auto_chain(
            _data_settings(),
            account=None,
            interval="60m",
            symbols=["000001.SH"],
        )
        self.assertNotIn(PROVIDER_BAOSTOCK, chain)
        # Remaining free providers that advertise index minute support stay.
        for name in (PROVIDER_MOOTDX, PROVIDER_AKSHARE):
            self.assertIn(name, chain)

    def test_stock_60m_keeps_baostock(self) -> None:
        chain = _resolve_auto_chain(
            _data_settings(),
            account=None,
            interval="60m",
            symbols=["600519.SH"],
        )
        self.assertIn(PROVIDER_BAOSTOCK, chain)

    def test_daily_index_keeps_baostock(self) -> None:
        chain = _resolve_auto_chain(
            _data_settings(),
            account=None,
            interval="1d",
            symbols=["000001.SH"],
        )
        self.assertIn(PROVIDER_BAOSTOCK, chain)

    def test_without_interval_keeps_default_order(self) -> None:
        chain = _resolve_auto_chain(_data_settings(), account=None)
        # No QMT account / no tushare token → free providers in _AUTO_PRIORITY order.
        self.assertEqual(
            chain[:3],
            [PROVIDER_BAOSTOCK, PROVIDER_MOOTDX, PROVIDER_AKSHARE],
        )
        self.assertNotIn("qmt", chain)

    def test_qmt_account_still_leads_when_interval_supported(self) -> None:
        account = MagicMock()
        account.has_connection = True
        chain = _resolve_auto_chain(
            _data_settings(),
            account=account,
            interval="60m",
            symbols=["600519.SH"],
        )
        self.assertEqual(chain[0], "qmt")


if __name__ == "__main__":
    unittest.main()
