import unittest

from doyoutrade.skills import load_skills


class StrategySkillsTests(unittest.TestCase):
    def test_strategy_authoring_skill_surface_is_loadable(self) -> None:
        skills = {skill.name: skill for skill in load_skills(enabled_only=True)}

        # Core authoring + iteration skills must always be present — AI
        # agents authoring strategies rely on this set being routable. The
        # former `strategy-sdk-cheatsheet` reference was folded into
        # `strategy-definition-authoring/references/` in 2026-05.
        self.assertIn("strategy-authoring", skills)
        self.assertIn("strategy-definition-authoring", skills)
        self.assertIn("strategy-iteration", skills)
        self.assertNotIn("strategy-sdk-cheatsheet", skills)

        authoring = skills["strategy-authoring"]
        # strategy-authoring is a process skill — it should route to the
        # definition-authoring skill for concrete contract + SDK details.
        self.assertIn("strategy-definition-authoring", authoring.body)

        definition = skills["strategy-definition-authoring"]
        # The definition-authoring skill must document the Strategy
        # base class, the on_bar entry point, and the compile_strategy_draft
        # in-process dry-run tool (replaced validate_strategy_code in Task 6
        # of the strategy-as-files refactor).
        self.assertIn("Strategy", definition.body)
        self.assertIn("on_bar", definition.body)
        self.assertIn("compile_strategy_draft", definition.body)

        self.assertIn("backtest", skills["strategy-iteration"].body.lower())

    def test_strategy_skill_uses_current_authoring_and_schema_contracts(self) -> None:
        skills = {skill.name: skill for skill in load_skills(enabled_only=True)}

        body = skills["doyoutrade-strategy"].body
        # StrategyInstance / ``si-`` bindings were removed; tasks bind a
        # definition (``sd-…``) directly via strategy bind / promote.
        self.assertIn("doyoutrade-cli schema strategy.bind", body)
        self.assertIn("doyoutrade-cli strategy bind", body)
        self.assertIn("doyoutrade-cli strategy promote", body)
        self.assertIn("--definition sd-", body)
        self.assertNotIn("strategy instance create", body)
        self.assertNotIn("--source-file", body)
        self.assertNotIn("--class-name", body)


if __name__ == "__main__":
    unittest.main()
