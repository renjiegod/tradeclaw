import unittest
from unittest.mock import patch, MagicMock
from doyoutrade.assistant.slash_commands import (
    resolve_skill_command_key,
    build_skill_invocation_message,
    _normalize_skill_key,
)


class TestNormalizeSkillKey(unittest.TestCase):
    def test_lowercase(self):
        assert _normalize_skill_key("Technical-Basic") == "technical-basic"

    def test_whitespace(self):
        assert _normalize_skill_key("  technical-basic  ") == "technical-basic"

    def test_underscore_to_hyphen(self):
        assert _normalize_skill_key("technical_basic") == "technical-basic"


class TestResolveSkillCommandKey(unittest.TestCase):
    def test_slash_command(self):
        result = resolve_skill_command_key("/technical-basic")
        assert result == "technical-basic"

    def test_slash_with_args(self):
        result = resolve_skill_command_key("/technical-basic momentum")
        assert result == "technical-basic"

    def test_uppercase_normalized(self):
        result = resolve_skill_command_key("/TECHNICAL-BASIC")
        assert result == "technical-basic"

    def test_no_slash_returns_none(self):
        result = resolve_skill_command_key("technical-basic")
        assert result is None

    def test_empty_returns_none(self):
        result = resolve_skill_command_key("")
        assert result is None

    def test_nonexistent_returns_none(self):
        result = resolve_skill_command_key("/nonexistent-skill-xyz")
        assert result is None


class TestBuildSkillInvocationMessage(unittest.TestCase):
    def test_basic_invocation(self):
        with patch("doyoutrade.assistant.slash_commands._load_skill_payload") as mock_load:
            mock_skill = MagicMock()
            mock_skill.name = "technical-basic"
            mock_skill.body = "## Technical Basic\nSkill content here."
            mock_skill.skill_dir = "/path/to/skills/technical-basic"
            mock_load.return_value = mock_skill

            result = build_skill_invocation_message("technical-basic", None)

            assert "<invoke_skill_loaded" in result
            assert 'skill="technical-basic"' in result
            assert "[IMPORTANT:" in result
            assert "The user has invoked" in result
            assert "## Technical Basic" in result
            assert "[Skill directory:" in result
            assert "</invoke_skill_loaded>" in result

    def test_with_args(self):
        with patch("doyoutrade.assistant.slash_commands._load_skill_payload") as mock_load:
            mock_skill = MagicMock()
            mock_skill.name = "technical-basic"
            mock_skill.body = "## Technical Basic"
            mock_skill.skill_dir = "/path/to/skills/technical-basic"
            mock_load.return_value = mock_skill

            result = build_skill_invocation_message("technical-basic", "momentum")

            assert "[User instruction: momentum]" in result

    def test_skill_not_found_returns_none(self):
        with patch("doyoutrade.assistant.slash_commands._load_skill_payload") as mock_load:
            mock_load.return_value = None
            result = build_skill_invocation_message("nonexistent", None)
            assert result is None
