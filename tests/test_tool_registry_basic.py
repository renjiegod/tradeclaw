"""Locks in the post-2026-05-25 tool-registry contract.

Per the architectural decision recorded in
``doyoutrade/tools/__init__.py::build_default_tool_registry``: the agent
sees two categories of tools:

1. Framework primitives: ``load_skill``, ``compact``,
   ``execute_bash``, ``manage_bash_tasks``.
2. File primitives: ``read_file``, ``write_file``, ``edit_file``,
   ``list_files`` — ``read_file`` is unrestricted (can read any path);
   ``write_file`` / ``edit_file`` enforce sandbox via the
   _sandbox registry; lifecycle tools (open/cancel/compile/finalize)
   register roots. ``knowledge_index`` is the knowledge-base navigation
   primitive in this family — a read-only, always-fresh index (one line
   per file) over ``~/.doyoutrade/knowledge`` that lets the agent reason
   over the structure first instead of blind ``list_files`` + per-file
   ``read_file``. It is a pure local file derivation, not a domain op.

Every domain operation (task / cron / strategy / backtest / cycle /
data / sdk / stock / pattern / factor / validation / model route) is
reached by shelling out to ``doyoutrade-cli`` via ``execute_bash``.

If you find yourself adding a non-file tool here, stop. The new
functionality should ship as a ``doyoutrade-cli`` subcommand instead.
"""

from __future__ import annotations

import unittest

from doyoutrade.tools import build_default_tool_registry


_EXPECTED_TOOL_NAMES = frozenset({
    "load_skill",
    "compact",
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "knowledge_index",
    "execute_bash",
    "manage_bash_tasks",
    "ask_user_question",
    "watch_job",
})


class BasicToolRegistryTests(unittest.TestCase):
    def test_registry_contains_only_expected_tools_with_no_args(self) -> None:
        registry = build_default_tool_registry()
        self.assertEqual(set(registry.names), _EXPECTED_TOOL_NAMES)

    def test_tool_result_max_chars_param_is_honored(self) -> None:
        registry = build_default_tool_registry(tool_result_max_chars=12345)
        self.assertEqual(registry._tool_result_max_chars, 12345)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
