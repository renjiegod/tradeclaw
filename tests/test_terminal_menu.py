"""Unit tests for the zero-dep arrow-key / numbered terminal menu."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from doyoutrade.terminal_menu import select_index


class TerminalMenuFallbackTests(unittest.TestCase):
    """When stdin is not a TTY (or raw mode fails), fall back to numbered input."""

    def test_numbered_choice_returns_zero_based_index(self):
        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=False), patch(
            "builtins.input", return_value="2"
        ):
            self.assertEqual(select_index("pick", ["a", "b", "c"]), 1)

    def test_numbered_zero_skips_when_allowed(self):
        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=False), patch(
            "builtins.input", return_value="0"
        ):
            self.assertIsNone(select_index("pick", ["a", "b"], allow_skip=True))

    def test_numbered_invalid_returns_none(self):
        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=False), patch(
            "builtins.input", return_value="99"
        ):
            self.assertIsNone(select_index("pick", ["a", "b"]))

    def test_skip_disallowed_rejects_zero(self):
        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=False), patch(
            "builtins.input", return_value="0"
        ):
            self.assertIsNone(select_index("pick", ["a"], allow_skip=False))


class TerminalMenuArrowTests(unittest.TestCase):
    def test_enter_selects_highlighted(self):
        # Simulate: down once, then enter → index 1
        keys = iter(["down", "enter"])

        def fake_read_key():
            return next(keys)

        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=True), patch(
            "doyoutrade.terminal_menu._read_key", side_effect=fake_read_key
        ), patch("doyoutrade.terminal_menu._draw_menu"), patch(
            "doyoutrade.terminal_menu._clear_menu_lines"
        ):
            self.assertEqual(select_index("pick", ["a", "b", "c"]), 1)

    def test_skip_option_via_arrow(self):
        # options + skip row; highlight starts at 0; down×2 lands on skip when 2 options
        keys = iter(["down", "down", "enter"])

        def fake_read_key():
            return next(keys)

        with patch("doyoutrade.terminal_menu._can_use_arrow_menu", return_value=True), patch(
            "doyoutrade.terminal_menu._read_key", side_effect=fake_read_key
        ), patch("doyoutrade.terminal_menu._draw_menu"), patch(
            "doyoutrade.terminal_menu._clear_menu_lines"
        ):
            self.assertIsNone(select_index("pick", ["a", "b"], allow_skip=True))


if __name__ == "__main__":
    unittest.main()
