"""Tests for ``doyoutrade-cli strategy definition list``.

This command calls the resource-oriented OpenAPI list endpoint and
post-processes its envelope to expose definitions. The tests cover the
projection helper and the click command end-to-end (via ``CliRunner``) with
a stubbed API invocation so we don't pay HTTP/runtime cost.

Why this test exists: session ``asst-f9826c84c5fd`` showed the model
reaching for ``strategy definition list`` (the obvious CRUD verb) before
discovering ``strategy inspect``. We added the ``list`` subcommand as a thin
filtered wrapper so the natural verb lands on a working command on the first
try.

StrategyInstance / ``si-`` bindings were removed; there is no
``strategy instance list`` command any more.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

# Import the strategy group through main so that ``backtest_runs``'s
# side-effect import registers ``strategy inspect`` too — the list
# command deliberately leaves inspect untouched, so we want both
# available in the test runner.
from doyoutrade.cli.commands.strategy import strategy as strategy_group
from doyoutrade.cli.commands import backtest_runs  # noqa: F401 — registers `strategy inspect`
from doyoutrade.cli._envelope import EXIT_OK


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class _DefStub:
    """Mimic the subset of ``StrategyDefinitionSnapshot`` the tool reads."""

    definition_id: str
    name: str
    class_name: str = "GeneratedStrategy"
    source_code: str = ""
    code_hash: str = ""
    generation_prompt: str = ""
    status: str = "active"
    created_at: datetime = _NOW


class _DefinitionRepoStub:
    def __init__(self, definitions: list[_DefStub]) -> None:
        self._definitions = definitions

    async def list_definitions(self) -> list[_DefStub]:
        return list(self._definitions)


def _make_runtime() -> dict[str, Any]:
    """Build a runtime dict with definitions covering both list-cases.

    Two macd-flavoured definitions and one rsi definition. Picked so the
    ``--query macd`` test filters down to two macd defs.
    """

    definitions = [
        _DefStub(
            definition_id="sd-macd-a",
            name="MACD Trend A",
            source_code="# macd a\n",
            code_hash="hash-macd-a",
            generation_prompt="macd crossover trend",
        ),
        _DefStub(
            definition_id="sd-macd-b",
            name="MACD Trend B",
            source_code="# macd b\n",
            code_hash="hash-macd-b",
            generation_prompt="macd crossover variant",
        ),
        _DefStub(
            definition_id="sd-rsi",
            name="RSI Revert",
            source_code="# rsi\n",
            code_hash="hash-rsi",
            generation_prompt="rsi mean reversion",
        ),
    ]
    return {
        "strategy_definition_repository": _DefinitionRepoStub(definitions),
        # ``aclose`` is invoked by ``run_async_command``; provide a no-op.
        "aclose": lambda: None,
    }


class StrategyListCommandTests(unittest.TestCase):
    """Drive ``strategy definition list`` via Click."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._fake = self._fake_invoke_api
        self._patchers = [
            patch("doyoutrade.cli.commands.strategy.invoke_api", new=self._fake),
            patch("doyoutrade.cli.commands.backtest_runs.invoke_api", new=self._fake),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self._patchers:
            patcher.stop()

    @staticmethod
    async def _fake_invoke_api(method: str, path: str, *, params=None, meta=None, **kwargs):
        runtime = _make_runtime()
        if method != "GET":
            raise AssertionError(f"unexpected method: {method}")
        if path == "/strategy-definitions":
            definitions = await runtime["strategy_definition_repository"].list_definitions()
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "definition_id": item.definition_id,
                            "name": item.name,
                            "code_hash": item.code_hash,
                            "generation_prompt": item.generation_prompt,
                            "status": item.status,
                        }
                        for item in definitions
                    ]
                },
            }, EXIT_OK
        raise AssertionError(f"unexpected path: {path}")

    def _run(self, args: list[str]):
        return self.runner.invoke(
            strategy_group,
            args,
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

    # ------------------------------------------------------------------
    # `strategy definition list` cases
    # ------------------------------------------------------------------

    def test_definition_list_returns_all_defs_without_instances(self) -> None:
        result = self._run(["definition", "list"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        data = envelope["data"]
        ids = [d["definition_id"] for d in data["definitions"]]
        self.assertEqual(sorted(ids), ["sd-macd-a", "sd-macd-b", "sd-rsi"])
        # instance projection no longer exists.
        self.assertNotIn("instances", data)
        self.assertNotIn("total_instances", data)
        self.assertIn("_summary", data)
        self.assertIn("Listed 3 definition(s)", data["_summary"])

    def test_definition_list_query_filters_and_includes_matched_tokens(self) -> None:
        result = self._run(["definition", "list", "--query", "macd"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        data = envelope["data"]
        ids = sorted(d["definition_id"] for d in data["definitions"])
        self.assertEqual(ids, ["sd-macd-a", "sd-macd-b"])
        self.assertEqual(data["matched_tokens"], ["macd"])
        self.assertEqual(data["query"], "macd")
        self.assertEqual(data["total_definitions"], 3)
        self.assertNotIn("total_instances", data)
        self.assertNotIn("instances", data)
        self.assertIn("query='macd'", data["_summary"])

    def test_definition_list_limit_truncates(self) -> None:
        result = self._run(["definition", "list", "--limit", "2"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        data = envelope["data"]
        self.assertEqual(len(data["definitions"]), 2)
        self.assertIn("Listed 2 definition(s)", data["_summary"])
        self.assertIn("limit=2", data["_summary"])

    # ------------------------------------------------------------------
    # `strategy inspect` cases (definitions only; si- layer removed)
    # ------------------------------------------------------------------

    def test_strategy_inspect_lists_definitions_without_instances_endpoint(self) -> None:
        result = self._run(["inspect"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        data = envelope["data"]
        ids = sorted(d["definition_id"] for d in data["definitions"])
        self.assertEqual(ids, ["sd-macd-a", "sd-macd-b", "sd-rsi"])
        self.assertEqual(data["status"], "ok")
        self.assertNotIn("instances", data)
        self.assertNotIn("total_instances", data)

    def test_strategy_inspect_query_filters_by_name(self) -> None:
        result = self._run(["inspect", "--query", "macd"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        data = envelope["data"]
        ids = sorted(d["definition_id"] for d in data["definitions"])
        self.assertEqual(ids, ["sd-macd-a", "sd-macd-b"])
        self.assertEqual(data["matched_tokens"], ["macd"])
        self.assertEqual(data["total_definitions"], 3)
        self.assertIn("match_reasons", data["definitions"][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
