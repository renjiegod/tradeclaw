"""Unit tests for doyoutrade.cli._kwargs — the click → tool kwargs adapters."""

from __future__ import annotations

import unittest

from doyoutrade.cli._kwargs import (
    merge_flat_over_params,
    parse_params_json,
    split_csv,
)


class ParseParamsJsonTests(unittest.TestCase):
    def test_none_input_returns_none_pair(self) -> None:
        params, err = parse_params_json(None)

        self.assertIsNone(params)
        self.assertIsNone(err)

    def test_empty_string_is_treated_as_absent(self) -> None:
        params, err = parse_params_json("")

        self.assertIsNone(params)
        self.assertIsNone(err)

    def test_valid_object_returns_dict(self) -> None:
        params, err = parse_params_json('{"agent": {"react_max_turns": 3}}')

        self.assertEqual(params, {"agent": {"react_max_turns": 3}})
        self.assertIsNone(err)

    def test_malformed_json_returns_envelope(self) -> None:
        params, err = parse_params_json("{not_valid")

        self.assertIsNone(params)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertFalse(err["ok"])
        self.assertEqual(err["error"]["error_code"], "invalid_params_json")
        self.assertIn("--params must be valid JSON", err["error"]["message"])

    def test_non_object_json_is_rejected(self) -> None:
        params, err = parse_params_json('["not", "an", "object"]')

        self.assertIsNone(params)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"]["error_code"], "invalid_params_json")
        self.assertIn("must be a JSON object", err["error"]["message"])


class SplitCsvTests(unittest.TestCase):
    def test_none_stays_none(self) -> None:
        self.assertIsNone(split_csv(None))

    def test_simple_split(self) -> None:
        self.assertEqual(split_csv("a,b,c"), ["a", "b", "c"])

    def test_trims_whitespace_and_drops_empty(self) -> None:
        self.assertEqual(split_csv(" a , , b ,"), ["a", "b"])


class MergeFlatOverParamsTests(unittest.TestCase):
    def test_flat_wins_over_params(self) -> None:
        params = {"name": "from_params", "mode": "paper"}
        flat = {"name": "from_flag", "description": "desc"}

        out = merge_flat_over_params(params, flat)

        self.assertEqual(out["name"], "from_flag")
        self.assertEqual(out["mode"], "paper")
        self.assertEqual(out["description"], "desc")

    def test_none_in_flat_does_not_overwrite(self) -> None:
        params = {"name": "kept"}
        flat = {"name": None, "mode": "new"}

        out = merge_flat_over_params(params, flat)

        self.assertEqual(out["name"], "kept")
        self.assertEqual(out["mode"], "new")

    def test_no_params_returns_flat_minus_none(self) -> None:
        out = merge_flat_over_params(None, {"name": "x", "mode": None})

        self.assertEqual(out, {"name": "x"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
