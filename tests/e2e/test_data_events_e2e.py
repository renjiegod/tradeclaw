"""E2E: ``data_events`` runs end-to-end against a real bootstrap.

Verifies registry wiring through the live runtime stack with an injected
in-memory event provider. Exhaustive coverage lives in
``tests.test_data_events``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from tests.e2e.support import E2EModelMode, build_e2e_runtime, e2e_enabled
from doyoutrade.api.cli_tools import build_cli_tool_registry
from doyoutrade.api.operations.data_events import DataEventsTool
from doyoutrade.core.models import EventItem
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities


def _run(coro):
    return asyncio.run(coro)


class _InMemoryEvents:
    capabilities = ProviderCapabilities(name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset())

    async def get_events_batch(self, symbols, *, asof=None):
        return {
            "600519.SH": [EventItem(code="600519.SH", event_type="suspension",
                                    event_date=asof or "", detail="重大事项", provider="akshare")]
        } if "600519.SH" in symbols else {}

    async def get_events(self, symbol, *, asof=None):
        return (await self.get_events_batch([symbol], asof=asof)).get(symbol, [])


@unittest.skipUnless(e2e_enabled(), "DOYOUTRADE_E2E=1 not set; skipping e2e suite")
class DataEventsE2E(unittest.TestCase):
    def test_registry_contains_data_events_and_runs(self) -> None:
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
                tool = registry.get("data_events")
                self.assertIsNotNone(tool, "data_events not registered")
                assert tool is not None
                self.assertIsInstance(tool, DataEventsTool)
                tool._event_provider_factory = lambda _ds: _InMemoryEvents()  # type: ignore[attr-defined]

                with tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, "e.csv")
                    result = await tool.execute(symbols="600519.SH,000001.SZ", asof="2026-05-29", output_path=out)
                    self.assertFalse(result.is_error, msg=result.text)
                    self.assertIn("\"symbols_with_events\": 1", result.text)
                    self.assertTrue(Path(out).exists())
                    self.assertIn("suspension", Path(out).read_text())

        _run(_run_test())


if __name__ == "__main__":
    unittest.main()
