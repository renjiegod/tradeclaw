"""E2E: ``data_fundamentals`` runs end-to-end against a real bootstrap.

Verifies the operation is wired into ``build_cli_tool_registry`` against the
live runtime stack. Uses an injected in-memory fundamentals provider so the
test doesn't depend on akshare / qmt-proxy availability. Exhaustive coverage
lives in ``tests.test_data_fundamentals``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from tests.e2e.support import E2EModelMode, build_e2e_runtime, e2e_enabled
from doyoutrade.api.cli_tools import build_cli_tool_registry
from doyoutrade.api.operations.data_fundamentals import DataFundamentalsTool
from doyoutrade.core.models import Fundamentals
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities


def _run(coro):
    return asyncio.run(coro)


class _InMemoryFundamentals:
    capabilities = ProviderCapabilities(name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset())

    async def get_fundamentals_batch(self, symbols, *, asof=None):
        return {
            s: Fundamentals(code=s, float_mv=2.1e12, total_mv=2.1e12, pe=30.0, pb=8.0,
                            price=1700.0, provider="akshare")
            for s in symbols
        }

    async def get_fundamentals(self, symbol, *, asof=None):
        return (await self.get_fundamentals_batch([symbol])).get(symbol)


@unittest.skipUnless(e2e_enabled(), "DOYOUTRADE_E2E=1 not set; skipping e2e suite")
class DataFundamentalsE2E(unittest.TestCase):
    def test_registry_contains_data_fundamentals_and_runs(self) -> None:
        async def _run_test() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                runtime = ctx.runtime
                registry = build_cli_tool_registry(
                    service=runtime["service"],
                    strategy_registry_service=runtime.get("strategy_registry_service"),
                    strategy_definition_repository=runtime.get("strategy_definition_repository"),
                    cron_manager=runtime.get("cron_manager"),
                    cron_run_repo=runtime.get("cron_run_repo"),
                    strategy_storage=runtime.get("strategy_storage"),
                    compiler=runtime.get("strategy_compiler"),
                )
                tool = registry.get("data_fundamentals")
                self.assertIsNotNone(tool, "data_fundamentals not registered")
                assert tool is not None
                self.assertIsInstance(tool, DataFundamentalsTool)
                tool._fundamentals_provider_factory = lambda _ds: _InMemoryFundamentals()  # type: ignore[attr-defined]

                with tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, "f.csv")
                    result = await tool.execute(symbols="600519.SH,000858.SZ", output_path=out)
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"symbols_matched\": 2", result.text)
                    self.assertTrue(Path(out).exists())
                    self.assertIn("float_mv", Path(out).read_text())

        _run(_run_test())


if __name__ == "__main__":
    unittest.main()
