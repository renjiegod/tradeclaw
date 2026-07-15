"""Unit tests for the identifier-kind guard abstraction.

StrategyInstance / ``si-`` bindings have been removed; the only prefixed
identifier kind is ``sd-`` (DEFINITION_ID). Everything else (including the
historical ``si-`` values) falls through to the uuid-family TASK_ID.
"""

from __future__ import annotations

import unittest

from doyoutrade.tools._identifier_kinds import (
    IdentifierGuard,
    IdentifierKind,
    apply_identifier_guards,
    check_identifier_kind,
    detect_identifier_kind,
)


class DetectIdentifierKindTests(unittest.TestCase):
    def test_detects_definition_id(self) -> None:
        self.assertEqual(
            detect_identifier_kind("sd-abc123"), IdentifierKind.DEFINITION_ID
        )

    def test_si_prefix_falls_through_to_task_id(self) -> None:
        # ``si-`` is no longer a known prefix; it classifies as TASK_ID.
        self.assertEqual(detect_identifier_kind("si-abc123"), IdentifierKind.TASK_ID)

    def test_uuid_falls_through_to_task_id(self) -> None:
        uuid_like = "b5bf9730-76f2-4ad3-b037-00154dec2734"
        self.assertEqual(detect_identifier_kind(uuid_like), IdentifierKind.TASK_ID)


class CheckIdentifierKindTests(unittest.TestCase):
    def test_matching_task_id_returns_none(self) -> None:
        self.assertIsNone(
            check_identifier_kind(
                "uuid-style", IdentifierKind.TASK_ID, field="task_id"
            )
        )

    def test_task_id_accepts_si_prefix(self) -> None:
        # ``si-...`` now classifies as TASK_ID, so it is accepted where a
        # task_id is required (no longer a wrong-identifier error).
        self.assertIsNone(
            check_identifier_kind("si-abc", IdentifierKind.TASK_ID, field="task_id")
        )

    def test_definition_id_rejects_task_id_value(self) -> None:
        err = check_identifier_kind(
            "uuid-style-task-id",
            IdentifierKind.DEFINITION_ID,
            field="definition_id",
        )
        assert err is not None
        self.assertEqual(err["error_code"], "wrong_identifier_type")
        self.assertEqual(err["error_type"], "WrongIdentifierType")
        self.assertEqual(err["expected_kind"], "definition_id")
        self.assertEqual(err["actual_kind"], "task_id")
        self.assertEqual(err["field"], "definition_id")

    def test_task_id_rejects_sd_prefix(self) -> None:
        err = check_identifier_kind(
            "sd-abc", IdentifierKind.TASK_ID, field="task_id"
        )
        assert err is not None
        self.assertEqual(err["expected_kind"], "task_id")
        self.assertEqual(err["actual_kind"], "definition_id")
        self.assertIn("get_strategy_definition", err["repair_hints"][1])

    def test_none_value_is_skipped(self) -> None:
        self.assertIsNone(
            check_identifier_kind(None, IdentifierKind.TASK_ID, field="task_id")
        )

    def test_empty_string_value_is_skipped(self) -> None:
        self.assertIsNone(
            check_identifier_kind("   ", IdentifierKind.TASK_ID, field="task_id")
        )


class ApplyGuardsTests(unittest.TestCase):
    def test_first_failing_guard_short_circuits(self) -> None:
        guards = (
            IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
            IdentifierGuard(field="task_id", kind=IdentifierKind.TASK_ID),
        )
        err = apply_identifier_guards(
            {"definition_id": "uuid-not-sd", "task_id": "sd-also-wrong"},
            guards,
        )
        assert err is not None
        self.assertEqual(err["field"], "definition_id")
        self.assertEqual(err["expected_kind"], "definition_id")

    def test_all_matching_guards_return_none(self) -> None:
        guards = (
            IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
            IdentifierGuard(field="task_id", kind=IdentifierKind.TASK_ID),
        )
        self.assertIsNone(
            apply_identifier_guards(
                {
                    "definition_id": "sd-abc",
                    "task_id": "uuid-style",
                },
                guards,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
