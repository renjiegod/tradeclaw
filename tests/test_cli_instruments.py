"""`doyoutrade-cli instruments catalog sync` CLI wiring.

Regression coverage for the "empty instrument_catalog on a fresh deployment"
gap: previously the only way to populate the table was a raw
``POST /instruments/catalog/sync`` call with no CLI surface, so an
agent/operator hitting a "symbols not in instrument catalog" error had no
discoverable path to fix it. This pins the new command's argument -> JSON
body translation.
"""

import json
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from doyoutrade.cli._envelope import EXIT_OK, EXIT_VALIDATION
from doyoutrade.cli.commands.instruments import instruments as instruments_group


class InstrumentsCatalogSyncCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.calls: list[tuple[str, str, dict | None]] = []
        self._patch = patch(
            "doyoutrade.cli.commands.instruments.invoke_api",
            new=self._fake_invoke_api,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    async def _fake_invoke_api(self, method: str, path: str, *, json=None, meta=None, **kwargs):
        self.calls.append((method, path, json))
        return ({"ok": True, "data": {"synced": 0}}, EXIT_OK)

    def _invoke(self, args: list[str]):
        return self.runner.invoke(
            instruments_group, args, obj={"fmt": "json"}, catch_exceptions=False
        )

    def test_full_sync_posts_source_and_mode_with_null_symbols(self) -> None:
        result = self._invoke(
            ["catalog", "sync", "--source", "akshare", "--mode", "full"]
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(
            self.calls,
            [
                (
                    "POST",
                    "/instruments/catalog/sync",
                    {"source": "akshare", "mode": "full", "symbols": None},
                )
            ],
        )

    def test_symbols_mode_collects_repeated_symbol_flags(self) -> None:
        result = self._invoke(
            [
                "catalog",
                "sync",
                "--source",
                "akshare",
                "--mode",
                "symbols",
                "--symbol",
                "600519.SH",
                "--symbol",
                "000001.SH",
            ]
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(
            self.calls,
            [
                (
                    "POST",
                    "/instruments/catalog/sync",
                    {
                        "source": "akshare",
                        "mode": "symbols",
                        "symbols": ["600519.SH", "000001.SH"],
                    },
                )
            ],
        )

    def test_symbols_mode_without_any_symbol_is_a_validation_error(self) -> None:
        result = self._invoke(["catalog", "sync", "--source", "akshare", "--mode", "symbols"])

        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "validation_error")
        self.assertEqual(self.calls, [])

    def test_rejects_unknown_source(self) -> None:
        result = self._invoke(["catalog", "sync", "--source", "bloomberg", "--mode", "full"])

        self.assertNotEqual(result.exit_code, EXIT_OK)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
