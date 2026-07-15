"""Unit tests for the shared kwargs-contract layer.

These tests exercise :func:`enforce_kwargs_contract` directly without any
specific tool, so the contract behaves the same regardless of which tool
opts into it. ``CreateTaskTool``-level integration coverage lives in
``tests/test_assistant_tools_task_manage.py``.
"""

from __future__ import annotations

import unittest

from doyoutrade.tools._contract import (
    LegacyLift,
    coerce_object_payload,
    coerce_string_array_payload,
    enforce_kwargs_contract,
)


def _strategy_lift() -> LegacyLift:
    return LegacyLift(
        target_path="settings.strategy",
        coerce=coerce_object_payload("strategy"),
        json_string_error="strategy must be an object or JSON object string",
        meta_moved_key="moved_top_level_strategy",
        meta_was_json_string_key="strategy_was_json_string",
    )


def _universe_lift() -> LegacyLift:
    return LegacyLift(
        target_path="settings.universe",
        coerce=coerce_string_array_payload("universe"),
        json_string_error="universe must be an array of strings or JSON array string",
        meta_moved_key="moved_top_level_universe",
        meta_was_json_string_key="universe_was_json_string",
    )


_ALLOWED = frozenset({"name", "settings", "mode", "description"})
_SUGGESTED = {
    "universe": "settings.universe",
    "strategy": "settings.strategy",
    "agent": "settings.agent",
    "strategy_preferences": "settings.strategy_preferences",
}


class ContractEnforcementTests(unittest.TestCase):
    # --- legacy lift: success paths -------------------------------------

    def test_lift_moves_top_level_universe_into_settings(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "universe": ["300058.SZ"],
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["settings"]["universe"], ["300058.SZ"])
        self.assertNotIn("universe", result.kwargs)
        self.assertEqual(
            result.legacy_normalization,
            {"moved_top_level_universe": True, "universe_was_json_string": False},
        )

    def test_lift_parses_json_string_array_for_universe(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "universe": '["300058.SZ"]',
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["settings"]["universe"], ["300058.SZ"])
        self.assertTrue(result.legacy_normalization["universe_was_json_string"])

    def test_lift_parses_json_string_object_for_strategy(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}},
                "strategy": '{"instance_id":"si-1"}',
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"strategy": _strategy_lift()},
        )
        self.assertIsNone(result.error)
        self.assertEqual(
            result.kwargs["settings"]["strategy"], {"instance_id": "si-1"}
        )
        self.assertTrue(result.legacy_normalization["strategy_was_json_string"])

    def test_lift_does_not_mutate_caller_settings(self) -> None:
        original_settings = {"agent": {}, "strategy": {"instance_id": "si-1"}}
        original_kwargs = {
            "name": "t",
            "settings": original_settings,
            "universe": ["300058.SZ"],
        }
        result = enforce_kwargs_contract(
            original_kwargs,
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertIsNone(result.error)
        self.assertNotIn("universe", original_settings)
        self.assertIn("universe", original_kwargs)  # caller's dict untouched

    # --- legacy lift: validation failures -------------------------------

    def test_lift_rejects_malformed_json_string(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "universe": "[300058",
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertEqual(result.error_kind, "validation_error")
        self.assertIn(
            "universe must be an array of strings",
            result.error["message"],  # type: ignore[index]
        )

    def test_lift_rejects_non_string_array_items(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "universe": [123, "300058.SZ"],
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertEqual(result.error_kind, "validation_error")
        self.assertIn(
            "universe must be an array of strings",
            result.error["message"],  # type: ignore[index]
        )

    def test_lift_rejects_conflicting_top_level_and_nested(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {
                    "agent": {},
                    "strategy": {"instance_id": "si-1"},
                    "universe": ["000001.SZ"],
                },
                "universe": ["300058.SZ"],
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertEqual(result.error_kind, "validation_error")
        self.assertIn(
            "cannot provide both top-level universe and settings.universe",
            result.error["message"],  # type: ignore[index]
        )

    def test_lift_rejects_when_settings_missing(self) -> None:
        result = enforce_kwargs_contract(
            {"name": "t", "universe": ["300058.SZ"]},
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"universe": _universe_lift()},
        )
        self.assertEqual(result.error_kind, "validation_error")
        self.assertIn("settings must be an object", result.error["message"])  # type: ignore[index]

    # --- unknown_arguments rejection ------------------------------------

    def test_unknown_arguments_with_suggested_path(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "strategy_preferences": "be aggressive",
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={},
        )
        self.assertEqual(result.error_kind, "unknown_arguments")
        err = result.error
        assert err is not None
        self.assertEqual(err["unknown"], ["strategy_preferences"])
        self.assertEqual(
            err["suggested_path"],
            {"strategy_preferences": "settings.strategy_preferences"},
        )
        self.assertEqual(
            sorted(err["allowed_top_level"]),
            ["description", "mode", "name", "settings"],
        )
        self.assertIn("settings.strategy_preferences", err["message"])
        self.assertIn("hint", err)

    def test_unknown_arguments_without_suggested_path(self) -> None:
        result = enforce_kwargs_contract(
            {
                "name": "t",
                "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
                "totally_unrelated": True,
            },
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={},
        )
        self.assertEqual(result.error_kind, "unknown_arguments")
        err = result.error
        assert err is not None
        self.assertEqual(err["unknown"], ["totally_unrelated"])
        self.assertNotIn("suggested_path", err)
        self.assertNotIn("hint", err)

    # --- happy path -----------------------------------------------------

    def test_happy_path_returns_kwargs_unchanged_when_valid(self) -> None:
        kwargs = {
            "name": "t",
            "mode": "backtest",
            "settings": {"agent": {}, "strategy": {"instance_id": "si-1"}},
        }
        result = enforce_kwargs_contract(
            kwargs,
            allowed_top_level=_ALLOWED,
            suggested_paths=_SUGGESTED,
            legacy_lifts={"strategy": _strategy_lift(), "universe": _universe_lift()},
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.legacy_normalization, {})
        # Kwargs is shallow-copied; identity must differ but content matches.
        self.assertIsNot(result.kwargs, kwargs)
        self.assertEqual(result.kwargs, kwargs)


class ContractIntegrationViaOperationHandlerTests(unittest.TestCase):
    """Smoke-test that OperationHandler.``_enforce_kwargs_contract`` derives
    the allowlist and ``settings.*`` suggested paths from the tool's
    schema without per-tool boilerplate.

    Tests use a minimal fake tool so they exercise the *helper layer*
    rather than coupling to any specific tool's schema.
    """

    def _make_tool_with_settings(self) -> object:
        """Return a fake tool whose schema has a nested ``settings`` property."""
        from doyoutrade.tools import OperationHandler

        class _FakeToolWithSettings(OperationHandler):
            name = "fake_nested"
            description = "test"
            parameters = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "settings": {
                        "type": "object",
                        "properties": {
                            "alpha": {"type": "string"},
                            "beta": {"type": "integer"},
                        },
                    },
                },
                "additionalProperties": False,
            }

            async def execute(self, **_kwargs):  # type: ignore[override]
                return ""

        return _FakeToolWithSettings()

    def _make_flat_tool(self) -> object:
        """Return a fake tool whose schema is fully flat (no ``settings`` wrapper)."""
        from doyoutrade.tools import OperationHandler

        class _FakeFlatTool(OperationHandler):
            name = "fake_flat"
            description = "test"
            parameters = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "mode": {"type": "string"},
                    "universe": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            }

            async def execute(self, **_kwargs):  # type: ignore[override]
                return ""

        return _FakeFlatTool()

    def test_default_suggested_paths_returns_settings_subkeys_when_settings_property_present(self) -> None:
        """When the schema has a ``settings`` object, each nested key maps to ``settings.<key>``."""
        tool = self._make_tool_with_settings()
        suggested = tool._suggested_kwarg_paths()  # type: ignore[union-attr]
        self.assertEqual(suggested.get("alpha"), "settings.alpha")
        self.assertEqual(suggested.get("beta"), "settings.beta")

    def test_default_suggested_paths_returns_empty_when_no_settings_property(self) -> None:
        """Flat schemas with no ``settings`` property yield an empty suggestions dict."""
        tool = self._make_flat_tool()
        suggested = tool._suggested_kwarg_paths()  # type: ignore[union-attr]
        self.assertEqual(suggested, {})

    def test_default_allowed_top_level_derives_from_schema_properties(self) -> None:
        """The allowlist is exactly the top-level ``properties`` keys declared on the schema."""
        tool = self._make_flat_tool()
        allowed = tool._allowed_top_level_kwargs()  # type: ignore[union-attr]
        self.assertEqual(sorted(allowed), ["mode", "name", "universe"])

    def test_base_class_defaults_are_empty(self) -> None:
        from doyoutrade.tools import OperationHandler

        # New optional class attributes are present with sane defaults so
        # legacy tools that never opt in keep working unchanged.
        self.assertEqual(OperationHandler.legacy_top_level_lifts, {})
        self.assertFalse(OperationHandler.accepts_extra_kwargs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
