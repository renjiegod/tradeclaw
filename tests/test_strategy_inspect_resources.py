"""Unit tests for shared strategy inspect projection."""

from __future__ import annotations

import unittest

from doyoutrade.strategies.inspect_resources import build_strategy_inspect_payload


class StrategyInspectResourcesTests(unittest.TestCase):
    def test_duplicate_groups_and_recommended_reuse(self) -> None:
        rows = [
            {
                "definition_id": "sd-dup-a",
                "name": "Dup A",
                "status": "active",
                "code_hash": "hash-shared",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "definition_id": "sd-dup-b",
                "name": "Dup B",
                "status": "active",
                "code_hash": "hash-shared",
                "created_at": "2026-01-02T00:00:00",
            },
            {
                "definition_id": "sd-unique",
                "name": "Unique",
                "status": "active",
                "code_hash": "hash-unique",
                "created_at": "2026-01-03T00:00:00",
            },
        ]

        payload = build_strategy_inspect_payload(rows)

        defs_by_id = {item["definition_id"]: item for item in payload["definitions"]}
        self.assertEqual(defs_by_id["sd-dup-a"]["recommended_reuse_id"], "sd-dup-a")
        self.assertEqual(defs_by_id["sd-dup-b"]["recommended_reuse_id"], "sd-dup-a")
        self.assertEqual(defs_by_id["sd-unique"]["recommended_reuse_id"], "sd-unique")
        groups = payload["duplicate_definition_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["recommended_reuse_id"], "sd-dup-a")

    def test_query_filters_and_surfaces_match_reasons(self) -> None:
        rows = [
            {
                "definition_id": "sd-macd",
                "name": "MACD Trend",
                "status": "active",
                "code_hash": "h1",
                "generation_prompt": "macd crossover",
            },
            {
                "definition_id": "sd-rsi",
                "name": "RSI Revert",
                "status": "active",
                "code_hash": "h2",
                "generation_prompt": "rsi mean reversion",
            },
        ]

        payload = build_strategy_inspect_payload(rows, query="macd trend")

        self.assertEqual([d["definition_id"] for d in payload["definitions"]], ["sd-macd"])
        self.assertEqual(payload["total_definitions"], 2)
        self.assertIn("name", payload["definitions"][0]["match_reasons"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
