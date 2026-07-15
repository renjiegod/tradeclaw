import unittest
from unittest.mock import patch, MagicMock


class TestGetAgentSkills(unittest.TestCase):
    def test_filters_by_name(self):
        with patch("doyoutrade.assistant.skill_preload.load_skills") as mock_load:
            s1 = MagicMock(enabled=True)
            s1.name = "technical-basic"
            s2 = MagicMock(enabled=True)
            s2.name = "backtest-diagnose"
            mock_load.return_value = [s1, s2]

            from doyoutrade.assistant.skill_preload import get_agent_skills
            result = get_agent_skills(["technical-basic"])
            assert len(result) == 1
            assert result[0].name == "technical-basic"

    def test_ignores_missing(self):
        with patch("doyoutrade.assistant.skill_preload.load_skills") as mock_load:
            s1 = MagicMock(enabled=True)
            s1.name = "technical-basic"
            mock_load.return_value = [s1]

            from doyoutrade.assistant.skill_preload import get_agent_skills
            result = get_agent_skills(["nonexistent"])
            assert len(result) == 0


class TestBuildPreloadedSkillsPrompt(unittest.TestCase):
    def test_lists_name_and_description_only(self):
        with patch("doyoutrade.assistant.skill_preload.get_agent_skills") as mock_get:
            s1 = MagicMock()
            s1.name = "technical-basic"
            s1.description = "Compute basic technical indicators."
            s1.body = "## Should not appear\nFull body content"
            s2 = MagicMock()
            s2.name = "backtest-diagnose"
            s2.description = "Diagnose backtest issues."
            s2.body = "Full body content"
            mock_get.return_value = [s1, s2]

            from doyoutrade.assistant.skill_preload import build_preloaded_skills_prompt
            result = build_preloaded_skills_prompt(["technical-basic", "backtest-diagnose"])

            assert "## Reference Skills" in result
            assert "load_skill" in result
            assert "documentation catalog" in result
            assert "- technical-basic: Compute basic technical indicators." in result
            assert "- backtest-diagnose: Diagnose backtest issues." in result
            # Full body must NOT be inlined
            assert "Full body content" not in result
            assert "## Should not appear" not in result
            # No legacy hermes-style wrappers
            assert "[IMPORTANT:" not in result
            assert "[Skill directory:" not in result

    def test_handles_missing_description(self):
        with patch("doyoutrade.assistant.skill_preload.get_agent_skills") as mock_get:
            s = MagicMock()
            s.name = "bare-skill"
            s.description = ""
            mock_get.return_value = [s]

            from doyoutrade.assistant.skill_preload import build_preloaded_skills_prompt
            result = build_preloaded_skills_prompt(["bare-skill"])
            assert "- bare-skill" in result
            assert "- bare-skill:" not in result

    def test_collapses_multiline_description(self):
        with patch("doyoutrade.assistant.skill_preload.get_agent_skills") as mock_get:
            s = MagicMock()
            s.name = "multi"
            s.description = "first line\nsecond line"
            mock_get.return_value = [s]

            from doyoutrade.assistant.skill_preload import build_preloaded_skills_prompt
            result = build_preloaded_skills_prompt(["multi"])
            assert "- multi: first line second line" in result

    def test_empty_skills_returns_empty_string(self):
        with patch("doyoutrade.assistant.skill_preload.get_agent_skills") as mock_get:
            mock_get.return_value = []

            from doyoutrade.assistant.skill_preload import build_preloaded_skills_prompt
            result = build_preloaded_skills_prompt([])
            assert result == ""
