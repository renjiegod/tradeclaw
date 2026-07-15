"""Tests for the validate_strategy_code dry-run assistant tool.

NOTE: ``ValidateStrategyCodeTool`` (``doyoutrade.tools.validate_strategy``)
was removed in Task 6 of the strategy-as-files refactor.  The equivalent
functionality is now ``compile_strategy_draft`` in the authoring lifecycle
(``doyoutrade.assistant.strategy_tools.authoring_tools``).

All tests in this file are skipped to preserve git history while avoiding
import errors.
"""

import unittest


@unittest.skip(
    "ValidateStrategyCodeTool (doyoutrade.tools.validate_strategy) removed in Task 6 "
    "(strategy-as-files refactor). Use compile_strategy_draft instead."
)
class ValidateStrategyCodeToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_well_formed_strategy(self) -> None:
        pass

    async def test_rejects_invalid_base_class(self) -> None:
        pass

    async def test_rejects_missing_on_bar(self) -> None:
        pass

    async def test_rejects_missing_required_arguments(self) -> None:
        pass

    async def test_rejects_unknown_top_level_kwarg(self) -> None:
        pass

    async def test_rejects_hallucinated_dp_method(self) -> None:
        pass

    async def test_rejects_missing_signal_tag(self) -> None:
        pass

    async def test_rejects_lookahead_access(self) -> None:
        pass

    async def test_smoke_passes_for_well_formed_macd_strategy(self) -> None:
        pass

    async def test_smoke_does_not_persist_side_effects(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
