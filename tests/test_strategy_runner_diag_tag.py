"""Regression tests for ``_resolve_diag_tag`` in ``doyoutrade.strategy_sdk.runner``.

Background: request1.json turn 2 false-diagnosed a zero-trade backtest
as "整个回测窗口都在策略预热期内" because the MACD reference's final
``return Signal.hold()`` had no tag — making the strategy's actual
decision invisible in ``strategy_runner_cycle.per_symbol_tags``. The
fix is two-pronged:

* runner now defaults to a ``<untagged_<direction>>`` sentinel so every
  symbol shows up in the timeline regardless of whether the author
  tagged the signal (this file)
* the MACD reference template was updated to tag every hold branch
  (verified by ``test_strategy_authoring_skill_surface_is_loadable``)

These tests pin the sentinel semantics so a future refactor can't
silently drop the fallback again.
"""

from __future__ import annotations

import unittest

from doyoutrade.strategy_sdk.runner import _resolve_diag_tag
from doyoutrade.strategy_sdk.signal import Signal


class ResolveDiagTagTests(unittest.TestCase):
    def test_tagged_hold_returns_user_tag(self) -> None:
        signal = Signal.hold(tag="no_cross")
        self.assertEqual(_resolve_diag_tag(signal), "no_cross")

    def test_untagged_hold_falls_back_to_sentinel(self) -> None:
        # ``Signal.hold()`` with no tag is the request1.json regression
        # case — it MUST surface in per_symbol_tags so operators can
        # diagnose "MACD valid but no cross" without re-running.
        signal = Signal.hold()
        self.assertEqual(_resolve_diag_tag(signal), "<untagged_hold>")

    def test_tagged_buy_returns_user_tag(self) -> None:
        signal = Signal.buy(tag="macd_golden_cross")
        self.assertEqual(_resolve_diag_tag(signal), "macd_golden_cross")

    def test_tagged_sell_returns_user_tag(self) -> None:
        signal = Signal.sell(tag="macd_dead_cross")
        self.assertEqual(_resolve_diag_tag(signal), "macd_dead_cross")

    def test_sentinel_includes_direction(self) -> None:
        # Even though buy/sell already require a tag at the SDK boundary,
        # the resolver still needs to round-trip them safely if someone
        # constructs a Signal directly (test or future internal use).
        bare_hold = Signal(direction=Signal.hold().direction)
        bare_buy = Signal(direction=Signal.buy(tag="x").direction)
        bare_sell = Signal(direction=Signal.sell(tag="x").direction)
        self.assertEqual(_resolve_diag_tag(bare_hold), "<untagged_hold>")
        self.assertEqual(_resolve_diag_tag(bare_buy), "<untagged_buy>")
        self.assertEqual(_resolve_diag_tag(bare_sell), "<untagged_sell>")


if __name__ == "__main__":
    unittest.main()
