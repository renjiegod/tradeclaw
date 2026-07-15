"""Unit tests for doyoutrade.cli._envelope.

Covers the contract surfaces skill docs reference:
* ``parse_tool_result`` extracting fenced JSON + error-prefix metadata.
* ``success_envelope`` / ``error_envelope`` shape contract.
* ``extract_unknown_arguments_fields`` round-tripping the prose from
  ``format_unknown_args``.
* ``exit_code_for_error`` mapping for the four canonical exit codes.
"""

from __future__ import annotations

import unittest

from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_NOT_FOUND,
    EXIT_VALIDATION,
    Meta,
    error_envelope,
    exit_code_for_error,
    extract_unknown_arguments_fields,
    parse_tool_result,
    success_envelope,
)
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)


class ParseToolResultTests(unittest.TestCase):
    def test_fenced_json_block_is_extracted_as_data(self) -> None:
        prose = "Task 550e8400 [running] MR Demo."
        payload = {"status": "ok", "task": {"task_id": "550e8400", "name": "MR Demo"}}
        text = append_json_payload(prose, payload)

        data, summary, error = parse_tool_result(text, is_error=False)

        self.assertEqual(data, payload)
        self.assertEqual(summary, prose)
        self.assertIsNone(error)

    def test_plain_text_without_fence_falls_through(self) -> None:
        text = "Created task 'X' (task_id=abc, mode=paper)."

        data, summary, error = parse_tool_result(text, is_error=False)

        self.assertIsNone(data)
        self.assertEqual(summary, text)
        self.assertIsNone(error)

    def test_error_prefix_is_parsed_when_is_error(self) -> None:
        text = format_error_text("wrong_identifier_type", "got 'si-x' looks like ...", "use get_task")

        data, summary, error = parse_tool_result(text, is_error=True)

        self.assertIsNone(data)
        self.assertIsNotNone(error)
        assert error is not None  # for mypy
        self.assertEqual(error["error_code"], "wrong_identifier_type")
        self.assertIn("looks like", error["message"])
        self.assertEqual(error["hint"], "use get_task")
        self.assertIn("wrong_identifier_type", summary)

    def test_error_without_prefix_falls_back_to_tool_error(self) -> None:
        text = "something went wrong"

        data, summary, error = parse_tool_result(text, is_error=True)

        self.assertEqual(error, {"error_code": "tool_error", "message": "something went wrong"})
        self.assertEqual(summary, "something went wrong")
        self.assertIsNone(data)

    def test_malformed_json_block_is_ignored(self) -> None:
        # Reproduce the rare case where the fence body isn't valid JSON.
        text = "header\n\n```json\n{not valid}\n```"

        data, summary, error = parse_tool_result(text, is_error=False)

        self.assertIsNone(data)
        # The full text round-trips as summary since we couldn't extract.
        self.assertIn("header", summary)
        self.assertIsNone(error)


class SuccessEnvelopeTests(unittest.TestCase):
    def test_minimum_envelope(self) -> None:
        env = success_envelope({"task_id": "abc"}, "ok", meta=Meta())

        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["task_id"], "abc")
        # No meta when Meta is empty.
        self.assertNotIn("meta", env)

    def test_meta_round_trips_only_set_fields(self) -> None:
        meta = Meta(agent_id="asst-1", session_id="s-1", debug_session_id="s-1")
        env = success_envelope({"task_id": "abc"}, "ok", meta=meta)

        self.assertEqual(env["meta"]["agent_id"], "asst-1")
        self.assertEqual(env["meta"]["session_id"], "s-1")
        self.assertEqual(env["meta"]["debug_session_id"], "s-1")
        self.assertNotIn("run_id", env["meta"])

    def test_summary_lands_under_data_when_no_summary_key(self) -> None:
        env = success_envelope({"items": [1, 2]}, "Two items.", meta=Meta())

        self.assertEqual(env["data"]["_summary"], "Two items.")

    def test_existing_summary_in_data_is_preserved(self) -> None:
        env = success_envelope({"_summary": "kept"}, "ignored", meta=Meta())

        self.assertEqual(env["data"]["_summary"], "kept")

    def test_data_none_drops_data_key(self) -> None:
        env = success_envelope(None, "", meta=Meta())

        self.assertNotIn("data", env)


class ErrorEnvelopeTests(unittest.TestCase):
    def test_minimum_envelope(self) -> None:
        env = error_envelope(
            error_code="wrong_identifier_type",
            message="bad shape",
            meta=Meta(),
        )

        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["error_code"], "wrong_identifier_type")
        self.assertEqual(env["error"]["message"], "bad shape")
        self.assertNotIn("error_type", env["error"])
        self.assertNotIn("meta", env)

    def test_repair_hints_and_extra_are_forwarded(self) -> None:
        env = error_envelope(
            error_code="ambiguous_task_name",
            error_type="AmbiguousTaskName",
            message="multiple matches",
            repair_hints=["pick one"],
            extra={"candidates": [{"task_id": "A"}, {"task_id": "B"}]},
            meta=Meta(session_id="s-1"),
        )

        self.assertEqual(env["error"]["error_type"], "AmbiguousTaskName")
        self.assertEqual(env["error"]["repair_hints"], ["pick one"])
        self.assertEqual(len(env["error"]["candidates"]), 2)
        self.assertEqual(env["meta"]["session_id"], "s-1")


class ExtractUnknownArgumentsTests(unittest.TestCase):
    def test_extracts_from_format_unknown_args_prose(self) -> None:
        # Round-trip the canonical producer.
        prose = format_unknown_args(["settings"], ["agent", "mode", "name", "strategy"], {})

        out = extract_unknown_arguments_fields(prose)

        self.assertEqual(out["unknown"], ["settings"])
        self.assertEqual(out["allowed_top_level"], ["agent", "mode", "name", "strategy"])
        self.assertNotIn("suggested_path", out)

    def test_extracts_suggested_rename_when_present(self) -> None:
        prose = format_unknown_args(
            ["universe"],
            ["name", "settings"],
            {"universe": "settings.universe"},
        )

        out = extract_unknown_arguments_fields(prose)

        self.assertEqual(out["unknown"], ["universe"])
        self.assertEqual(out["suggested_path"], {"universe": "settings.universe"})

    def test_unrelated_prose_returns_empty_dict(self) -> None:
        out = extract_unknown_arguments_fields("[error:other] something")

        self.assertEqual(out, {})


class ExitCodeForErrorTests(unittest.TestCase):
    def test_validation_codes(self) -> None:
        for code in (
            "unknown_arguments",
            "validation_error",
            "wrong_identifier_type",
            "missing_query",
            "missing_strategy_instance_id",
            "invalid_parameters_json",      # starts with invalid_
            "invalid_approval_policy_json",
        ):
            with self.subTest(code=code):
                self.assertEqual(exit_code_for_error(code), EXIT_VALIDATION)

    def test_not_found_codes(self) -> None:
        for code in ("task_not_found", "skill_not_found", "bash_task_not_found", "file_not_found", "unknown_source"):
            with self.subTest(code=code):
                self.assertEqual(exit_code_for_error(code), EXIT_NOT_FOUND)

    def test_unknown_code_falls_back_to_failure(self) -> None:
        self.assertEqual(exit_code_for_error("some_business_error"), EXIT_FAILURE)


class MetaTests(unittest.TestCase):
    def test_to_dict_omits_empty_fields(self) -> None:
        meta = Meta(agent_id="asst-1")

        self.assertEqual(meta.to_dict(), {"agent_id": "asst-1"})

    def test_extra_extends_dict(self) -> None:
        meta = Meta(agent_id="asst-1", extra={"trace_id": "abc"})

        self.assertEqual(meta.to_dict()["trace_id"], "abc")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
