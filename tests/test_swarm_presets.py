"""Swarm preset 加载与 inspect 测试（含打包可见性）。"""

from __future__ import annotations

import unittest

from doyoutrade.swarm.presets import (
    PRESETS_DIR,
    build_run_from_preset,
    inspect_preset,
    list_presets,
    load_preset,
)


class PresetLoadingTests(unittest.TestCase):
    def test_bundled_presets_are_discoverable(self) -> None:
        # 打包可见性：presets/ 必须随包存在且非空（hatchling 默认含包内非 .py）。
        self.assertTrue(PRESETS_DIR.exists())
        names = {p["name"] for p in list_presets()}
        self.assertIn("investment_committee", names)
        self.assertIn("quant_strategy_desk", names)

    def test_missing_preset_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_preset("no_such_preset")

    def test_build_run_initializes_blocked_state(self) -> None:
        run = build_run_from_preset(
            "investment_committee", {"target": "AAPL", "market": "US"}
        )
        self.assertTrue(run.id.startswith("swarm-"))
        self.assertEqual(len(run.agents), 4)
        by_id = {t.id: t for t in run.tasks}
        # 第一层无依赖 → pending；下游 → blocked
        self.assertEqual(by_id["task-bull"].status.value, "pending")
        self.assertEqual(by_id["task-risk"].status.value, "blocked")
        self.assertEqual(by_id["task-risk"].blocked_by, ["task-bull", "task-bear"])


class InspectPresetTests(unittest.TestCase):
    def test_investment_committee_valid_with_layers(self) -> None:
        info = inspect_preset("investment_committee")
        self.assertTrue(info["valid"], info["errors"])
        self.assertEqual(info["errors"], [])
        layer_ids = [[t["task_id"] for t in layer] for layer in info["layers"]]
        self.assertEqual(set(layer_ids[0]), {"task-bull", "task-bear"})
        self.assertEqual(layer_ids[-1], ["task-decision"])

    def test_quant_strategy_desk_valid(self) -> None:
        info = inspect_preset("quant_strategy_desk")
        self.assertTrue(info["valid"], info["errors"])
        self.assertEqual(sorted(info["variables"]), ["horizon", "universe"])


if __name__ == "__main__":
    unittest.main()
