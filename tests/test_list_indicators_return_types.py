"""Locks in the ``ListIndicatorsTool`` return-type surface.

Previously the tool emitted only ``{name, signature, doc}`` per
indicator. LLM-authored strategies guessed attribute names on the
returned NamedTuples (e.g. ``macd_trio.histogram`` instead of ``.hist``)
and bounced off the smoke gate with ``AttributeError``. The tool now
exposes ``return_type`` so the model can read the actual field names.
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest

from doyoutrade.api.operations.strategy_discovery import ListIndicatorsTool


def _parse_payload(text: str) -> dict:
    match = re.search(r"```json\n(.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise AssertionError(f"no fenced JSON payload in tool result: {text!r}")
    return json.loads(match.group(1))


def _by_name(entries: list[dict], name: str) -> dict:
    for entry in entries:
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"no indicator entry named {name!r}; got {[e.get('name') for e in entries]!r}")


class ListIndicatorsReturnTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        tool = ListIndicatorsTool()
        result = asyncio.run(tool.execute())
        self.assertFalse(result.is_error, msg=result.text)
        self.text = result.text
        self.payload = _parse_payload(result.text)
        self.entries = self.payload["indicators"]

    def test_status_ok(self) -> None:
        self.assertEqual(self.payload["status"], "ok")
        self.assertEqual(self.payload["tool"], "list_indicators")
        self.assertGreater(self.payload["count"], 0)

    def test_macd_return_type_exposes_namedtuple_fields(self) -> None:
        macd = _by_name(self.entries, "macd")
        self.assertIn("return_type", macd)
        rt = macd["return_type"]
        self.assertEqual(rt["type"], "MACDResult")
        self.assertIn("fields", rt)
        names = [f["name"] for f in rt["fields"]]
        # The whole point of this test: the histogram field is `hist`, not `histogram`.
        self.assertEqual(names, ["macd", "signal", "hist"])
        for field in rt["fields"]:
            self.assertEqual(field["type"], "pd.Series")
        # Field-doc parsing should pick up the per-bullet docstrings.
        hist_field = next(f for f in rt["fields"] if f["name"] == "hist")
        self.assertIn("doc", hist_field)
        self.assertIn("hist", hist_field["doc"].lower() + " " + hist_field["name"])

    def test_bollinger_return_type_exposes_upper_middle_lower(self) -> None:
        boll = _by_name(self.entries, "bollinger")
        self.assertIn("return_type", boll)
        rt = boll["return_type"]
        self.assertEqual(rt["type"], "BollingerResult")
        names = [f["name"] for f in rt["fields"]]
        self.assertEqual(names, ["upper", "middle", "lower"])

    def test_adx_return_type_exposes_adx_plus_minus_di(self) -> None:
        adx = _by_name(self.entries, "adx")
        self.assertIn("return_type", adx)
        rt = adx["return_type"]
        self.assertEqual(rt["type"], "ADXResult")
        names = [f["name"] for f in rt["fields"]]
        self.assertEqual(names, ["adx", "plus_di", "minus_di"])

    def test_extended_indicators_present(self) -> None:
        # The momentum/volume/channel/trend expansion must show up in the
        # public surface so the agent can discover them.
        names = {e.get("name") for e in self.entries}
        for expected in (
            "kdj",
            "cci",
            "vwap",
            "keltner",
            "supertrend",
            "ichimoku",
            "limit_up_approx",
            "limit_down_approx",
        ):
            self.assertIn(expected, names)

    def test_kdj_return_type_exposes_k_d_j(self) -> None:
        rt = _by_name(self.entries, "kdj")["return_type"]
        self.assertEqual(rt["type"], "KDJResult")
        self.assertEqual([f["name"] for f in rt["fields"]], ["k", "d", "j"])

    def test_supertrend_return_type_exposes_line_and_direction(self) -> None:
        rt = _by_name(self.entries, "supertrend")["return_type"]
        self.assertEqual(rt["type"], "SuperTrendResult")
        self.assertEqual([f["name"] for f in rt["fields"]], ["supertrend", "direction"])

    def test_zigzag_return_type_exposes_pivot_and_direction(self) -> None:
        rt = _by_name(self.entries, "zigzag")["return_type"]
        self.assertEqual(rt["type"], "ZigZagResult")
        self.assertEqual([f["name"] for f in rt["fields"]], ["pivot", "direction"])

    def test_sma_return_type_is_pd_series_no_fields(self) -> None:
        sma = _by_name(self.entries, "sma")
        self.assertIn("return_type", sma)
        rt = sma["return_type"]
        self.assertEqual(rt["type"], "pd.Series")
        self.assertNotIn("fields", rt)

    def test_rsi_return_type_is_pd_series(self) -> None:
        rsi = _by_name(self.entries, "rsi")
        self.assertEqual(rsi["return_type"]["type"], "pd.Series")
        self.assertNotIn("fields", rsi["return_type"])

    def test_header_mentions_return_type_fields(self) -> None:
        # The text before the JSON fence.
        header = self.text.split("\n\n```json")[0]
        self.assertIn("return_type.fields", header)

    def test_notes_include_namedtuple_warning(self) -> None:
        notes = self.payload["notes"]
        joined = " ".join(notes)
        self.assertIn("return_type.fields", joined)
        self.assertIn("MACDResult.hist", joined)
        self.assertIn(".histogram", joined)

    def test_entry_shape_preserves_signature_and_doc(self) -> None:
        macd = _by_name(self.entries, "macd")
        # Original keys are still there — we only added one.
        self.assertIn("signature", macd)
        self.assertTrue(macd["signature"].startswith("indicators.macd("))
        self.assertIn("doc", macd)
        self.assertTrue(macd["doc"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
