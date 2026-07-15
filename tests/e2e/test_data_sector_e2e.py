"""E2E: ``data_sector`` runs end-to-end against a real bootstrap.

Verifies the new operation is wired into ``build_cli_tool_registry`` against
the live runtime stack (a missing import / registry typo surfaces here, not
just in unit tests). Uses an injected in-memory sector provider so the test
doesn't depend on akshare / qmt-proxy availability.

Per CLAUDE.md §测试要求 §E2E, new API operations must have an E2E run that
exercises the registry + operation through the same code path the HTTP
server uses. Exhaustive mode / failure coverage lives in
``tests.test_data_sector``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.api.cli_tools import build_cli_tool_registry
from doyoutrade.api.operations.data_sector import DataSectorTool
from doyoutrade.core.models import SectorMember
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities


def _run(coro):
    return asyncio.run(coro)


class _InMemorySectorProvider:
    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset()
    )

    async def list_sectors(self, *, sector_type=None):
        return ["白酒", "半导体"]

    async def get_sector_members(self, sector_name, *, sector_type=None):
        return [
            SectorMember(sector_name=sector_name, code="600519.SH", name="贵州茅台",
                        provider="akshare", sector_type="industry"),
            SectorMember(sector_name=sector_name, code="000858.SZ", name="五粮液",
                        provider="akshare", sector_type="industry"),
        ]


@unittest.skipUnless(e2e_enabled(), "DOYOUTRADE_E2E=1 not set; skipping e2e suite")
class DataSectorE2E(unittest.TestCase):
    def test_registry_contains_data_sector_and_runs_through_bootstrap(self) -> None:
        async def _run_test() -> None:
            async with build_e2e_runtime(
                profile="isolated", model_mode=E2EModelMode.STUB
            ) as ctx:
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
                tool = registry.get("data_sector")
                self.assertIsNotNone(tool, "data_sector not registered in cli_tool_registry")
                assert tool is not None
                self.assertIsInstance(tool, DataSectorTool)

                tool._sector_provider_factory = lambda _ds: _InMemorySectorProvider()  # type: ignore[attr-defined]

                # List mode through the real runtime.
                list_result = await tool.execute(data_source="auto")
                self.assertFalse(list_result.is_error, msg=list_result.text)
                self.assertIn("白酒", list_result.text)

                # Members mode writes a screenable universe CSV.
                with tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, "universe.csv")
                    result = await tool.execute(sector_names="白酒", output_path=out)
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"universe_size\": 2", result.text)
                    self.assertTrue(Path(out).exists())
                    self.assertEqual(Path(out).read_text().split(), ["600519.SH", "000858.SZ"])

        _run(_run_test())


if __name__ == "__main__":
    unittest.main()
