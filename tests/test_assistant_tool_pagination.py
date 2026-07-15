from __future__ import annotations

import unittest

from doyoutrade.tools._pagination import (
    append_pagination_hint,
    format_pagination_hint,
)


class FormatPaginationHintTests(unittest.TestCase):
    def test_returns_none_when_no_more_items(self) -> None:
        # offset(0) + shown(20) >= total(20): last page.
        self.assertIsNone(
            format_pagination_hint(
                tool_name="list_tasks",
                total=20,
                shown=20,
                limit=20,
                offset=0,
            )
        )

    def test_returns_none_when_shown_is_zero(self) -> None:
        # Empty result set: nothing to paginate.
        self.assertIsNone(
            format_pagination_hint(
                tool_name="list_tasks",
                total=0,
                shown=0,
                limit=20,
                offset=0,
            )
        )

    def test_first_page_hint_basics(self) -> None:
        hint = format_pagination_hint(
            tool_name="list_tasks",
            total=350,
            shown=20,
            limit=20,
            offset=0,
        )
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertIn("330 more", hint)
        self.assertIn("list_tasks(", hint)
        self.assertIn("offset=20", hint)
        self.assertIn("limit=20", hint)
        self.assertTrue(hint.endswith("to see the next page."))

    def test_carries_over_non_none_filters_only(self) -> None:
        hint = format_pagination_hint(
            tool_name="list_tasks",
            total=350,
            shown=20,
            limit=20,
            offset=0,
            filters={"q": "alpha", "status": None, "mode": "paper"},
        )
        assert hint is not None
        self.assertIn("q='alpha'", hint)
        self.assertIn("mode='paper'", hint)
        self.assertNotIn("status=", hint)

    def test_filter_order_is_preserved_then_pagination(self) -> None:
        hint = format_pagination_hint(
            tool_name="list_tasks",
            total=100,
            shown=20,
            limit=20,
            offset=20,
            filters={"q": "alpha", "mode": "paper"},
        )
        assert hint is not None
        # Filters first, then pagination kwargs — gives the model a clean
        # copy-pastable invocation.
        q_pos = hint.find("q='alpha'")
        mode_pos = hint.find("mode='paper'")
        offset_pos = hint.find("offset=40")
        limit_pos = hint.find("limit=20")
        self.assertGreater(mode_pos, q_pos)
        self.assertGreater(offset_pos, mode_pos)
        self.assertGreater(limit_pos, offset_pos)

    def test_custom_offset_and_limit_param_names(self) -> None:
        hint = format_pagination_hint(
            tool_name="search_logs",
            total=500,
            shown=100,
            limit=100,
            offset=0,
            offset_param="start",
            limit_param="count",
        )
        assert hint is not None
        self.assertIn("start=100", hint)
        self.assertIn("count=100", hint)
        self.assertNotIn("offset=", hint)
        self.assertNotIn("limit=", hint)


class AppendPaginationHintTests(unittest.TestCase):
    def test_appends_blank_line_then_hint(self) -> None:
        lines = ["Found 20 task(s) of 350 total:", "- task-1", "- task-2"]
        append_pagination_hint(
            lines,
            tool_name="list_tasks",
            total=350,
            shown=20,
            limit=20,
            offset=0,
        )
        self.assertEqual(lines[-3], "- task-2")
        self.assertEqual(lines[-2], "")
        self.assertIn("330 more", lines[-1])

    def test_does_not_append_when_no_more(self) -> None:
        lines = ["Found 20 task(s) of 20 total:", "- task-1"]
        before = list(lines)
        append_pagination_hint(
            lines,
            tool_name="list_tasks",
            total=20,
            shown=20,
            limit=20,
            offset=0,
        )
        self.assertEqual(lines, before)

    def test_does_not_double_blank_line(self) -> None:
        # If caller already ended with an empty line, don't add another.
        lines = ["Header", "row", ""]
        append_pagination_hint(
            lines,
            tool_name="list_tasks",
            total=100,
            shown=20,
            limit=20,
            offset=0,
        )
        # Last three: 'row', '', '<hint>' — no extra blank inserted.
        self.assertEqual(lines[-3], "row")
        self.assertEqual(lines[-2], "")
        self.assertIn("80 more", lines[-1])


if __name__ == "__main__":
    unittest.main()
