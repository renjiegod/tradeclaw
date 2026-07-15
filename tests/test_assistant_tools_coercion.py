"""Unit tests for the schema-coercion abstraction."""

from __future__ import annotations

import unittest

from doyoutrade.tools._coercion import (
    SchemaCoercion,
    apply_schema_coercion,
)


class SchemaCoercionTests(unittest.TestCase):
    def test_object_field_passes_through_native_dict(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion({"config_overrides": {"a": 1}}, rules)
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["config_overrides"], {"a": 1})
        self.assertEqual(result.coerced_fields, [])

    def test_object_field_coerces_json_string(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion(
            {"config_overrides": '{"max_notional": 100000}'}, rules
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["config_overrides"], {"max_notional": 100000})
        self.assertEqual(result.coerced_fields, ["config_overrides"])

    def test_object_field_rejects_invalid_json_with_default_error_code(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion({"config_overrides": "{not json"}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_config_overrides_json")
        self.assertEqual(result.error["error_type"], "ValueError")
        self.assertIn("config_overrides", result.error["error"])
        self.assertIn("hint", result.error)

    def test_object_field_rejects_array_payload(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion({"config_overrides": "[]"}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_config_overrides_json")

    def test_array_of_strings_passes_native_list(self) -> None:
        rules = (
            SchemaCoercion(field="tags", declared_type="array", item_type=str),
        )
        result = apply_schema_coercion({"tags": ["a", "b"]}, rules)
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["tags"], ["a", "b"])

    def test_array_of_strings_coerces_json_string(self) -> None:
        rules = (
            SchemaCoercion(field="tags", declared_type="array", item_type=str),
        )
        result = apply_schema_coercion({"tags": '["a","b"]'}, rules)
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["tags"], ["a", "b"])
        self.assertEqual(result.coerced_fields, ["tags"])

    def test_array_of_strings_rejects_non_string_item(self) -> None:
        rules = (
            SchemaCoercion(field="tags", declared_type="array", item_type=str),
        )
        result = apply_schema_coercion({"tags": [1, "b"]}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_tags_json")
        self.assertIn("array of strings", result.error["error"])

    def test_missing_field_is_skipped(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion({"other": 1}, rules)
        self.assertIsNone(result.error)
        self.assertNotIn("config_overrides", result.kwargs)

    def test_none_value_is_skipped(self) -> None:
        rules = (SchemaCoercion(field="config_overrides", declared_type="object"),)
        result = apply_schema_coercion({"config_overrides": None}, rules)
        self.assertIsNone(result.error)
        self.assertIsNone(result.kwargs["config_overrides"])

    def test_custom_error_code_is_honored(self) -> None:
        rules = (
            SchemaCoercion(
                field="config_overrides",
                declared_type="object",
                error_code="bad_config",
            ),
        )
        result = apply_schema_coercion({"config_overrides": "junk"}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "bad_config")

    def test_first_failing_rule_short_circuits(self) -> None:
        rules = (
            SchemaCoercion(field="config_overrides", declared_type="object"),
            SchemaCoercion(field="tags", declared_type="array", item_type=str),
        )
        # Both fields invalid; only the first should appear in error.
        result = apply_schema_coercion(
            {"config_overrides": "{not json", "tags": [1]}, rules
        )
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_config_overrides_json")

    def test_boolean_field_passes_through_native_bool(self) -> None:
        rules = (SchemaCoercion(field="enabled", declared_type="boolean"),)
        result = apply_schema_coercion({"enabled": True}, rules)
        self.assertIsNone(result.error)
        self.assertEqual(result.kwargs["enabled"], True)
        self.assertEqual(result.coerced_fields, [])

    def test_boolean_field_coerces_string_true_case_insensitive(self) -> None:
        rules = (SchemaCoercion(field="enabled", declared_type="boolean"),)
        for raw in ("true", "True", "TRUE", " true ", "1"):
            with self.subTest(raw=raw):
                result = apply_schema_coercion({"enabled": raw}, rules)
                self.assertIsNone(result.error)
                self.assertIs(result.kwargs["enabled"], True)
                self.assertEqual(result.coerced_fields, ["enabled"])

    def test_boolean_field_coerces_string_false_case_insensitive(self) -> None:
        rules = (SchemaCoercion(field="enabled", declared_type="boolean"),)
        for raw in ("false", "False", "FALSE", " 0 "):
            with self.subTest(raw=raw):
                result = apply_schema_coercion({"enabled": raw}, rules)
                self.assertIsNone(result.error)
                self.assertIs(result.kwargs["enabled"], False)
                self.assertEqual(result.coerced_fields, ["enabled"])

    def test_boolean_field_rejects_other_strings(self) -> None:
        rules = (SchemaCoercion(field="enabled", declared_type="boolean"),)
        result = apply_schema_coercion({"enabled": "yes"}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_enabled_json")
        self.assertEqual(result.error["error_type"], "ValueError")
        self.assertIn("boolean", result.error["error"])
        self.assertIn("hint", result.error)

    def test_boolean_field_rejects_non_string_non_bool(self) -> None:
        rules = (SchemaCoercion(field="enabled", declared_type="boolean"),)
        result = apply_schema_coercion({"enabled": 42}, rules)
        assert result.error is not None
        self.assertEqual(result.error["error_code"], "invalid_enabled_json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
