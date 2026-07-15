"""Unit tests for per-backtest ``config_overrides`` merge."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from doyoutrade.persistence.repositories import TaskSnapshot
from doyoutrade.platform.backtest_config_merge import (
    build_cycle_task_config_with_backtest_overrides,
    normalize_backtest_config_overrides,
)


def _snapshot(**kwargs) -> TaskSnapshot:
    base = dict(
        task_id="inst-1",
        name="n",
        template_id="single-agent-trend",
        mode="backtest",
        orchestrator_mode="single-agent",
        description="",
        data_provider="mock",
        status="configured",
        last_error="",
        watch_symbols=("600000.SH",),
        universe=("600000.SH", "600519.SH"),
        execution_strategy="",
        account_id="",
        model_id="",
        model_route_name=None,
        settings={
            "react_max_turns": 3,
            "signal_tool_names": ["data_bars_relative"],
            "position_constraints": {"max_single_order_amount": 10000.0},
        },
        enabled_skills=(),
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    base.update(kwargs)
    return TaskSnapshot(**base)  # type: ignore[arg-type]


class BacktestConfigMergeTests(unittest.TestCase):
    def test_normalize_rejects_unknown_key(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_backtest_config_overrides({"foo": 1})
        self.assertIn("unknown keys", str(ctx.exception))

    def test_normalize_empty_settings_returns_none(self):
        self.assertIsNone(normalize_backtest_config_overrides({}))
        self.assertIsNone(normalize_backtest_config_overrides({"settings": {}}))

    def test_deep_merge_position_constraints(self):
        snap = _snapshot()
        ov = normalize_backtest_config_overrides(
            {
                "settings": {
                    "position_constraints": {"review_equity_fraction": 0.4},
                },
            },
        )
        assert ov is not None
        cfg = build_cycle_task_config_with_backtest_overrides(snap, ov)
        self.assertEqual(cfg.max_single_order_amount, 10000.0)
        self.assertAlmostEqual(cfg.review_equity_fraction, 0.4)

    def test_top_level_universe_overrides_settings(self):
        snap = _snapshot()
        ov = normalize_backtest_config_overrides({"universe": ["000001.SZ"]})
        assert ov is not None
        cfg = build_cycle_task_config_with_backtest_overrides(snap, ov)
        self.assertEqual(cfg.universe, ("000001.SZ",))

    def test_normalize_accepts_and_drops_watch_symbols_override(self):
        ov = normalize_backtest_config_overrides({"watch_symbols": ["600000.SH"]})
        self.assertIsNone(ov)

    def test_build_config_does_not_use_record_watch_symbols(self):
        snap = _snapshot(watch_symbols=("600000.SH",), universe=("000001.SZ",))
        cfg = build_cycle_task_config_with_backtest_overrides(snap, None)
        self.assertEqual(cfg.watch_symbols, ())
        self.assertEqual(cfg.universe, ("000001.SZ",))

    def test_signal_tools_override_via_settings(self):
        snap = _snapshot()
        ov = normalize_backtest_config_overrides(
            {"settings": {"signal_tool_names": ["data_bars_relative", "invoke_skill"]}},
        )
        assert ov is not None
        cfg = build_cycle_task_config_with_backtest_overrides(snap, ov)
        self.assertEqual(
            cfg.signal_tool_names,
            ("data_bars_relative", "invoke_skill"),
        )
