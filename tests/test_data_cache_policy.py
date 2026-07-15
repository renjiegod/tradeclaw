from __future__ import annotations

import unittest

from doyoutrade.data.cache_policy import (
    DEFAULT_SOURCE_PRIORITY,
    DataCachePolicy,
    parse_data_cache_policy,
)


class DataCachePolicyParseTests(unittest.TestCase):
    def test_defaults_when_empty_object(self) -> None:
        p = parse_data_cache_policy({})
        self.assertEqual(p.source_priority, DEFAULT_SOURCE_PRIORITY)
        self.assertTrue(p.local_first)
        self.assertTrue(p.auto_backfill)
        self.assertEqual(p.on_unverifiable_gap, "fail")

    def test_full_round_trip(self) -> None:
        p = parse_data_cache_policy(
            {
                "source_priority": ["baostock", "qmt", "baostock"],  # de-duped, order kept
                "local_first": False,
                "auto_backfill": False,
                "continuity": {"on_unverifiable_gap": "degrade"},
            }
        )
        self.assertEqual(p.source_priority, ("baostock", "qmt"))
        self.assertFalse(p.local_first)
        self.assertFalse(p.auto_backfill)
        self.assertEqual(p.on_unverifiable_gap, "degrade")
        # as_payload round-trips to plain JSON-able types.
        self.assertEqual(p.as_payload()["source_priority"], ["baostock", "qmt"])

    def test_non_dict_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "data_cache must be an object"):
            parse_data_cache_policy(["not", "a", "dict"])

    def test_unknown_top_level_field_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown field"):
            parse_data_cache_policy({"force_refesh": True})  # typo

    def test_removed_top_level_fields_rejected_as_unknown(self) -> None:
        # force_refresh / interval are no longer task-configurable. Per §错误可见性
        # they must fail fast as unknown fields rather than be silently ignored.
        for field, value in (("force_refresh", True), ("interval", "5m")):
            with self.assertRaisesRegex(ValueError, "unknown field"):
                parse_data_cache_policy({field: value})

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            parse_data_cache_policy({"source_priority": ["qmt", "nope"]})

    def test_empty_source_priority_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one provider"):
            parse_data_cache_policy({"source_priority": []})

    def test_source_priority_not_array_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an array"):
            parse_data_cache_policy({"source_priority": "qmt"})

    def test_non_bool_flag_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "local_first must be a boolean"):
            parse_data_cache_policy({"local_first": "yes"})

    def test_continuity_mode_rejected_as_unknown(self) -> None:
        # continuity.mode is no longer configurable (always calendar, with
        # automatic degradation when the served source has no authoritative
        # calendar); a stray mode key must be rejected, not silently dropped.
        with self.assertRaisesRegex(ValueError, "continuity has unknown field"):
            parse_data_cache_policy({"continuity": {"mode": "internal_gap"}})

    def test_bad_on_unverifiable_gap_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "on_unverifiable_gap must be one of"):
            parse_data_cache_policy({"continuity": {"on_unverifiable_gap": "maybe"}})

    def test_unknown_continuity_field_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuity has unknown field"):
            parse_data_cache_policy({"continuity": {"modo": "calendar"}})

    def test_continuity_not_dict_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuity must be an object"):
            parse_data_cache_policy({"continuity": "calendar"})

    def test_frozen_dataclass_is_immutable(self) -> None:
        p = DataCachePolicy()
        with self.assertRaises(Exception):
            p.local_first = False  # type: ignore[misc]

    def test_to_settings_block_round_trips_through_parse(self) -> None:
        p = parse_data_cache_policy(
            {
                "source_priority": ["baostock", "qmt"],
                "continuity": {"on_unverifiable_gap": "degrade"},
            }
        )
        block = p.to_settings_block()
        self.assertEqual(block["continuity"], {"on_unverifiable_gap": "degrade"})
        # The serialized block re-parses to an equal policy (no field lost).
        self.assertEqual(parse_data_cache_policy(block), p)


class SerializeConfigRoundTripTests(unittest.TestCase):
    """Regression for the API serializer dropping data_cache (caught only by the
    real-chain task create→get round-trip, not the runtime parse path)."""

    def test_serialize_config_echoes_data_cache(self) -> None:
        from doyoutrade.platform.service import _serialize_config
        from doyoutrade.runtime.cycle_task import cycle_task_config_from_params

        cfg = cycle_task_config_from_params(
            name="rt",
            mode="backtest",
            settings={
                "strategy": {"definition_id": "sd-x"},
                "data_cache": {
                    "source_priority": ["baostock", "qmt"],
                    "continuity": {"on_unverifiable_gap": "fail"},
                },
            },
        )
        out = _serialize_config(cfg)
        self.assertIn("data_cache", out)
        self.assertEqual(out["data_cache"]["source_priority"], ["baostock", "qmt"])
        self.assertEqual(
            out["data_cache"]["continuity"],
            {"on_unverifiable_gap": "fail"},
        )

    def test_serialize_config_omits_data_cache_when_unset(self) -> None:
        from doyoutrade.platform.service import _serialize_config
        from doyoutrade.runtime.cycle_task import cycle_task_config_from_params

        cfg = cycle_task_config_from_params(
            name="rt", mode="backtest", settings={"strategy": {"definition_id": "sd-x"}}
        )
        self.assertNotIn("data_cache", _serialize_config(cfg))


if __name__ == "__main__":
    unittest.main()
