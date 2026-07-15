"""Tests for :mod:`doyoutrade.runtime.cycle_task` config factory and defaults."""

import unittest

from doyoutrade.runtime.cycle_task import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    DEFAULT_LOT_SIZE,
    DEFAULT_MAX_POSITION_RATIO,
    DEFAULT_MIN_NOTIONAL_FOR_APPROVAL,
    DEFAULT_REACT_MAX_TURNS,
    DEFAULT_REBALANCE_HYSTERESIS_LOTS,
    DEFAULT_REVIEW_EQUITY_FRACTION,
    DEFAULT_SIGNAL_TOOL_NAMES,
    CycleTaskConfig,
    cycle_task_config_from_params,
    merge_task_settings,
    validate_api_task_settings,
)


class CycleTaskConfigMigrationTests(unittest.TestCase):
    """Tests for the 5 migrated fields: execution_strategy, account_id, model_id,
    watch_symbols, enabled_skills — stored IN settings, not as top-level columns."""

    def test_merge_task_settings_preserves_execution_strategy(self) -> None:
        merged = merge_task_settings({"execution_strategy": "langchain"})
        self.assertEqual(merged["execution_strategy"], "langchain")

    def test_merge_task_settings_preserves_account_id(self) -> None:
        merged = merge_task_settings({"account_id": "acct-123"})
        self.assertEqual(merged["account_id"], "acct-123")

    def test_merge_task_settings_preserves_model_id(self) -> None:
        merged = merge_task_settings({"model_id": "gpt-4o"})
        self.assertEqual(merged["model_id"], "gpt-4o")

    def test_merge_task_settings_drops_watch_symbols(self) -> None:
        merged = merge_task_settings({"watch_symbols": ["AAPL", "MSFT"]})
        self.assertNotIn("watch_symbols", merged)

    def test_merge_task_settings_preserves_enabled_skills(self) -> None:
        merged = merge_task_settings({"enabled_skills": ["skill-a", "skill-b"]})
        self.assertEqual(merged["enabled_skills"], ["skill-a", "skill-b"])

    def test_factory_reads_execution_strategy_from_settings(self) -> None:
        """execution_strategy in settings maps to strategy_preferences in config."""
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={"execution_strategy": "langchain", **{
                "react_max_turns": 1,
                "signal_tool_names": [],
                "model_route_name": "r1",
            }},
        )
        self.assertEqual(cfg.strategy_preferences, "langchain")

    def test_factory_ignores_watch_symbols_and_uses_universe(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            universe=["ignored"],
            settings={"watch_symbols": ["BTC", "ETH"], "universe": ["AAA", "BBB"], **{
                "react_max_turns": 1,
                "signal_tool_names": [],
                "model_route_name": "r1",
            }},
        )
        self.assertEqual(cfg.universe, ("AAA", "BBB"))

    def test_factory_reads_universe_from_settings(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            universe=["ignored"],
            settings={"universe": ["AAA", "BBB"], **{
                "react_max_turns": 1,
                "signal_tool_names": [],
                "model_route_name": "r1",
            }},
        )
        self.assertEqual(cfg.universe, ("AAA", "BBB"))

    def test_factory_reads_enabled_skills_from_settings(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={
                "agent": {
                    "enabled_skills": ["skill-a", "skill-b"],
                    "react_max_turns": 1,
                    "signal_tool_names": [],
                },
                "model_route_name": "r1",
            },
        )
        self.assertEqual(cfg.enabled_skills, ("skill-a", "skill-b"))

    def test_factory_defaults_enabled_skills_to_empty(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={
                "react_max_turns": 1,
                "signal_tool_names": [],
                "model_route_name": "r1",
            },
        )
        self.assertEqual(cfg.enabled_skills, ())


class CycleTaskConfigTests(unittest.TestCase):
    def test_factory_applies_builtin_defaults_without_settings(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings=None,
        )
        self.assertEqual(cfg.universe, ())
        self.assertIsNone(cfg.max_single_order_amount)
        self.assertEqual(cfg.max_position_ratio, DEFAULT_MAX_POSITION_RATIO)
        self.assertIsNone(cfg.max_task_position_amount)
        self.assertIsNone(cfg.max_task_position_ratio)
        self.assertEqual(cfg.min_notional_for_approval, DEFAULT_MIN_NOTIONAL_FOR_APPROVAL)
        self.assertEqual(cfg.approval_timeout_seconds, DEFAULT_APPROVAL_TIMEOUT_SECONDS)
        self.assertEqual(cfg.react_max_turns, DEFAULT_REACT_MAX_TURNS)
        self.assertEqual(cfg.signal_tool_names, DEFAULT_SIGNAL_TOOL_NAMES)
        self.assertEqual(cfg.review_equity_fraction, DEFAULT_REVIEW_EQUITY_FRACTION)

    def test_factory_reads_position_constraints_and_approval_from_settings(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="live",
            settings={
                "position_constraints": {
                    "max_single_order_amount": 1500.0,
                    "max_position_ratio": 0.08,
                    "review_equity_fraction": 0.25,
                    "max_task_position_amount": 4000.0,
                    "max_task_position_ratio": 0.4,
                },
                "approval": {
                    "min_notional_for_approval": 500.0,
                    "timeout_seconds": 120,
                },
                "model_route_name": "r1",
            },
        )
        self.assertEqual(cfg.max_single_order_amount, 1500.0)
        self.assertEqual(cfg.max_position_ratio, 0.08)
        self.assertEqual(cfg.review_equity_fraction, 0.25)
        self.assertEqual(cfg.max_task_position_amount, 4000.0)
        self.assertEqual(cfg.max_task_position_ratio, 0.4)
        self.assertEqual(cfg.min_notional_for_approval, 500.0)
        self.assertEqual(cfg.approval_timeout_seconds, 120)

    def test_factory_reads_agent_fields_from_settings(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={
                "agent": {
                    "react_max_turns": 4,
                    "signal_tool_names": ["data_bars_relative"],
                },
                "model_route_name": "r1",
            },
        )
        self.assertEqual(cfg.react_max_turns, 4)
        self.assertEqual(cfg.signal_tool_names, ("data_bars_relative",))

    def test_merge_task_settings_fills_missing_signal_keys(self) -> None:
        merged = merge_task_settings({"risk": "low"})
        self.assertEqual(merged["risk"], "low")
        self.assertEqual(merged["agent_react_max_turns"], DEFAULT_REACT_MAX_TURNS)
        self.assertEqual(merged["agent_signal_tool_names"], list(DEFAULT_SIGNAL_TOOL_NAMES))

    def test_merge_task_settings_drops_legacy_omit_prefetched_market(self) -> None:
        merged = merge_task_settings({"omit_prefetched_market": True})
        self.assertNotIn("omit_prefetched_market", merged)

    def test_validate_api_task_settings_requires_strategy_binding(self) -> None:
        """``strategy.definition_id`` 仍是必填；``agent`` 块整体可省，``signal_tool_names``
        / ``react_max_turns`` 也都可省（缺省走默认值）。"""
        base = {"strategy": {"definition_id": "sd-1"}}
        with self.assertRaises(ValueError):
            validate_api_task_settings({})
        with self.assertRaises(ValueError):
            validate_api_task_settings({"react_max_turns": 1})
        with self.assertRaises(ValueError):
            validate_api_task_settings({"react_max_turns": 1, "signal_tool_names": []})
        # ``agent`` 完全省略也合法（runtime 用 DEFAULT_SIGNAL_TOOL_NAMES 兜底）
        validate_api_task_settings(base)
        validate_api_task_settings(
            {
                "agent": {"react_max_turns": 1, "signal_tool_names": []},
                **base,
            }
        )
        validate_api_task_settings(
            {
                "agent": {"react_max_turns": 1, "signal_tool_names": []},
                "strategy": {"definition_id": "sd-1"},
                "model_route_name": "",
            }
        )

    def test_validate_api_task_settings_rejects_missing_strategy_binding(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "agent": {"react_max_turns": 1, "signal_tool_names": []},
                }
            )

    def test_validate_api_task_settings_strategy_definition_path(self) -> None:
        # definition_id is the only valid binding now (no XOR with instance_id).
        validate_api_task_settings(
            {
                "agent": {"react_max_turns": 1, "signal_tool_names": []},
                "strategy": {
                    "definition_id": "sd-demo",
                    "parameter_overrides": {"lookback": "20"},
                    "execution_profile": "default",
                },
            }
        )
        # A strategy block without definition_id is rejected.
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {
                        "parameter_overrides": {},
                    },
                }
            )

    def test_cycle_task_config_reads_strategy_definition_binding(self) -> None:
        cfg = cycle_task_config_from_params(
            name="definition-task",
            mode="paper",
            settings={
                "strategy": {
                    "definition_id": "sd-demo",
                    "parameter_overrides": {"lookback": "20"},
                },
            },
        )
        self.assertEqual(cfg.strategy_definition_id, "sd-demo")
        self.assertEqual(cfg.strategy_parameter_overrides, {"lookback": "20"})

    def test_cycle_task_config_reads_strategy_execution_profile(self) -> None:
        cfg = cycle_task_config_from_params(
            name="definition-task",
            mode="paper",
            settings={
                "model_route_name": "r1",
                "strategy": {
                    "definition_id": "sd-demo",
                    "parameter_overrides": {"lookback": "20"},
                    "execution_profile": "default",
                },
            },
        )
        self.assertEqual(cfg.strategy_definition_id, "sd-demo")
        self.assertEqual(cfg.strategy_parameter_overrides, {"lookback": "20"})
        self.assertEqual(cfg.strategy_execution_profile, "default")

    def test_merge_task_settings_expands_agent_block(self) -> None:
        """Nested ``agent`` block is expanded into flat ``agent_*`` keys."""
        merged = merge_task_settings(
            {
                "agent": {
                    "react_max_turns": 8,
                    "signal_tool_names": ["invoke_skill", "data_bars_relative"],
                    "enabled_skills": ["skill-a"],
                },
            }
        )
        self.assertEqual(merged["agent_react_max_turns"], 8)
        self.assertEqual(merged["agent_signal_tool_names"], ["invoke_skill", "data_bars_relative"])
        self.assertEqual(merged["agent_enabled_skills"], ["skill-a"])

    def test_merge_task_settings_expands_agent_block_with_nested_position_constraints(self) -> None:
        """Nested ``agent.position_constraints`` and ``agent.approval`` are expanded."""
        merged = merge_task_settings(
            {
                "agent": {
                    "react_max_turns": 3,
                    "signal_tool_names": ["invoke_skill"],
                    "position_constraints": {
                        "max_single_order_amount": 5000.0,
                        "max_position_ratio": 0.15,
                        "review_equity_fraction": 0.4,
                        "max_task_position_amount": 25000.0,
                        "max_task_position_ratio": 0.35,
                    },
                    "approval": {
                        "min_notional_for_approval": 800.0,
                        "timeout_seconds": 180,
                    },
                },
            }
        )
        self.assertEqual(merged["agent_react_max_turns"], 3)
        self.assertEqual(merged["agent_pc_max_single_order_amount"], 5000.0)
        self.assertEqual(merged["agent_pc_max_position_ratio"], 0.15)
        self.assertEqual(merged["agent_pc_review_equity_fraction"], 0.4)
        self.assertEqual(merged["agent_pc_max_task_position_amount"], 25000.0)
        self.assertEqual(merged["agent_pc_max_task_position_ratio"], 0.35)
        self.assertEqual(merged["agent_approval_min_notional"], 800.0)
        self.assertEqual(merged["agent_approval_timeout"], 180)

    def test_validate_api_task_settings_agent_mode_requires_fields(self) -> None:
        """Agent 块整体可省；显式给的字段才校验格式（``react_max_turns`` >= 1，
        ``signal_tool_names`` 为字符串数组）。"""
        base = {
            "agent": {"react_max_turns": 1, "signal_tool_names": []},
            "strategy": {"definition_id": "sd-1"},
        }
        validate_api_task_settings(base)
        # react_max_turns / signal_tool_names 都缺省也合法
        validate_api_task_settings(
            {"agent": {}, "strategy": {"definition_id": "sd-1"}}
        )
        validate_api_task_settings(
            {"agent": {"signal_tool_names": []}, "strategy": {"definition_id": "sd-1"}}
        )
        validate_api_task_settings(
            {"agent": {"react_max_turns": 1}, "strategy": {"definition_id": "sd-1"}}
        )
        with self.assertRaises(ValueError):
            validate_api_task_settings({})
        # 显式给了但格式错的仍然拒绝
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "agent": {"react_max_turns": 0, "signal_tool_names": []},
                    "strategy": {"definition_id": "sd-1"},
                }
            )
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "agent": {"signal_tool_names": "trade"},  # 非数组
                    "strategy": {"definition_id": "sd-1"},
                }
            )

    def test_dataclass_field_defaults_match_module_constants(self) -> None:
        cfg = CycleTaskConfig(
            name="x",
            mode="paper",
        )
        self.assertIsNone(cfg.max_single_order_amount)
        self.assertEqual(cfg.max_position_ratio, DEFAULT_MAX_POSITION_RATIO)
        self.assertEqual(cfg.min_notional_for_approval, DEFAULT_MIN_NOTIONAL_FOR_APPROVAL)
        self.assertEqual(cfg.approval_timeout_seconds, DEFAULT_APPROVAL_TIMEOUT_SECONDS)
        self.assertEqual(cfg.react_max_turns, DEFAULT_REACT_MAX_TURNS)
        self.assertEqual(cfg.signal_tool_names, DEFAULT_SIGNAL_TOOL_NAMES)
        self.assertEqual(cfg.enabled_skills, ())
        self.assertEqual(cfg.review_equity_fraction, DEFAULT_REVIEW_EQUITY_FRACTION)
        self.assertEqual(cfg.strategy_definition_id, "")
        self.assertEqual(cfg.strategy_parameter_overrides, {})
        self.assertEqual(cfg.strategy_execution_profile, "default")

    def test_validate_api_task_settings_rejects_invalid_review_equity_fraction(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "react_max_turns": 1,
                    "signal_tool_names": [],
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"review_equity_fraction": 1.5},
                }
            )

    def test_validate_api_task_settings_rejects_invalid_task_budget_constraints(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"max_task_position_amount": 0},
                }
            )
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"max_task_position_ratio": 1.5},
                }
            )

    def test_validate_api_task_settings_accepts_nested_agent_task_budget_constraints(self) -> None:
        validate_api_task_settings(
            {
                "strategy": {"definition_id": "sd-1"},
                "agent": {
                    "position_constraints": {
                        "max_task_position_amount": 50000,
                        "max_task_position_ratio": 0.5,
                    }
                },
            }
        )


class CycleTaskLotConstraintTests(unittest.TestCase):
    """``lot_size`` / ``rebalance_hysteresis_lots`` parse + validate + default."""

    def test_defaults_when_omitted(self) -> None:
        cfg = cycle_task_config_from_params(name="n", mode="paper")
        self.assertEqual(cfg.lot_size, DEFAULT_LOT_SIZE)
        self.assertEqual(cfg.lot_size, 1)
        self.assertEqual(cfg.rebalance_hysteresis_lots, DEFAULT_REBALANCE_HYSTERESIS_LOTS)
        self.assertEqual(cfg.rebalance_hysteresis_lots, 0)

    def test_root_level_position_constraints_parsed(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={
                "position_constraints": {
                    "lot_size": 100,
                    "rebalance_hysteresis_lots": 2,
                }
            },
        )
        self.assertEqual(cfg.lot_size, 100)
        self.assertEqual(cfg.rebalance_hysteresis_lots, 2)

    def test_agent_block_position_constraints_parsed(self) -> None:
        cfg = cycle_task_config_from_params(
            name="n",
            mode="paper",
            settings={
                "agent": {
                    "position_constraints": {
                        "lot_size": 200,
                        "rebalance_hysteresis_lots": 1,
                    }
                }
            },
        )
        self.assertEqual(cfg.lot_size, 200)
        self.assertEqual(cfg.rebalance_hysteresis_lots, 1)

    def test_merge_expands_lot_constraints_from_agent_block(self) -> None:
        merged = merge_task_settings(
            {
                "agent": {
                    "position_constraints": {
                        "lot_size": 100,
                        "rebalance_hysteresis_lots": 3,
                    }
                }
            }
        )
        self.assertEqual(merged["agent_pc_lot_size"], 100)
        self.assertEqual(merged["agent_pc_rebalance_hysteresis_lots"], 3)

    def test_validate_rejects_non_integer_lot_size(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"lot_size": 100.5},
                }
            )

    def test_validate_rejects_lot_size_below_one(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"lot_size": 0},
                }
            )

    def test_validate_rejects_negative_hysteresis(self) -> None:
        with self.assertRaises(ValueError):
            validate_api_task_settings(
                {
                    "strategy": {"definition_id": "sd-1"},
                    "position_constraints": {"rebalance_hysteresis_lots": -1},
                }
            )

    def test_validate_accepts_valid_lot_constraints(self) -> None:
        validate_api_task_settings(
            {
                "strategy": {"definition_id": "sd-1"},
                "position_constraints": {
                    "lot_size": 100,
                    "rebalance_hysteresis_lots": 0,
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
