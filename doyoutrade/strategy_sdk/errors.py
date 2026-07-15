"""Strategy SDK error hierarchy and stable ``error_code`` token catalog.

Every failure mode that crosses the SDK / strategy / compiler boundary returns
a typed exception carrying a stable ``error_code`` token. The token is part of
the public contract — once shipped it must not change because:

- :mod:`doyoutrade.strategy_runtime.compiler` AST checks raise these to the
  authoring agent, and the ``strategy-definition-authoring`` skill documents
  ``error_code`` → repair recipe lookups by name.
- Assistant tools (``validate_strategy_draft`` etc.) surface ``error_code`` in
  their structured response so the LLM can branch on it.
- Debug events emit ``error_code`` in their payload so operators can grep
  failures by category.

Adding a new code is fine; renaming or repurposing an existing one is a
breaking change for skill docs and any historical debug events.
"""

from __future__ import annotations

from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Compile-time codes (raised by StrategyCompiler.validate / AST passes).
# Bypass attempts in user strategy code MUST surface one of these.
# ---------------------------------------------------------------------------

#: ``import requests`` / ``import akshare`` / ``import doyoutrade.data.*``
#: All data access must go through ``ctx.dp.*``.
DISALLOWED_IMPORT = "disallowed_import"

#: A class that inherits from :class:`Strategy` did not implement ``on_bar``.
MISSING_ON_BAR = "missing_on_bar"

#: ``Signal.buy()`` / ``Signal.sell()`` called without ``tag=`` keyword.
#: Tag is mandatory so debug session / trade_fills can attribute the decision.
MISSING_SIGNAL_TAG = "missing_signal_tag"

#: Inside ``on_bar``, ``df.iloc[N]`` where ``N >= 0``, or ``df.shift(-n)`` —
#: anything that reads forward in time. The current bar is always ``iloc[-1]``.
LOOKAHEAD_ACCESS = "lookahead_access"

#: Inside ``populate_indicators``, ``ctx.dp.get_bars(symbol=other_symbol)``
#: was called. Cross-symbol data must be declared via ``informative_data``
#: and read in ``on_bar``; populate_indicators is per-symbol vectorized.
POPULATE_CROSS_SYMBOL_ACCESS = "populate_cross_symbol_access"

#: ``except Exception: pass`` / ``except: pass`` / silent ``continue`` after
#: bare except. CLAUDE.md "错误可见性" forbids this.
SILENT_EXCEPTION_SWALLOW = "silent_exception_swallow"

#: ``if not isinstance(x, T): x = default`` patterns / try-except-TypeError
#: that swallows shape mismatch into a default value.
SILENT_TYPE_COERCION = "silent_type_coercion"

#: ``ctx.dp.foo(...)`` where ``foo`` is not a registered method.
UNKNOWN_DP_METHOD = "unknown_dp_method"

#: ``DataRequest.foo(...)`` factory that does not exist.
UNKNOWN_DATA_REQUEST_TYPE = "unknown_data_request_type"

#: An ``@informative('1w')`` decorated method had the wrong shape (signature
#: mismatch, unknown timeframe, missing return type).
INVALID_INFORMATIVE_DECORATOR = "invalid_informative_decorator"

#: ``startup_history`` literal contradicts an explicit ``len(df) < N`` /
#: ``rolling(N)`` literal inside the strategy body. Same rule as the legacy
#: compiler's ``required_history`` drift check.
HISTORY_CHECK_LITERAL_DISALLOWED = "history_check_literal_disallowed"

#: A class attribute on :class:`Strategy` was declared with a wrong type
#: (e.g. ``timeframe = 123``, ``can_short = "yes"``).
INVALID_CLASS_ATTRIBUTE = "invalid_class_attribute"

#: ``@informative`` declared cyclic dependencies between timeframes.
CIRCULAR_INFORMATIVE_DEPENDENCY = "circular_informative_dependency"


# ---------------------------------------------------------------------------
# Runtime codes (raised by ctx.dp.* / runner / informative prefetch).
# Surfaced as DataAccessError / RuntimeStrategyError instances.
# ---------------------------------------------------------------------------

#: ``ctx.dp.get_bars(window=N)`` could not return ``N`` rows.
DATA_INSUFFICIENT = "data_insufficient"

#: ``symbol`` argument did not match any known instrument.
INVALID_SYMBOL = "invalid_symbol"

#: ``window`` / ``top_n`` / similar argument failed validation.
INVALID_ARGUMENT = "invalid_argument"

#: ``Signal.sell(exit_reason=...)`` received a value outside the
#: :class:`doyoutrade.strategy_sdk.signal.ExitReason` enum. Rejected (not
#: silently coerced) so a typo'd reason cannot pollute exit attribution.
INVALID_EXIT_REASON = "invalid_exit_reason"

#: ``Signal.sell(fraction=...)`` outside ``(0, 1]``. ``fraction`` is the
#: portion of the held position to sell (``1.0`` = full exit, the default).
#: Rejected at compile (literal) and construction (computed) time — never
#: clamped — so an LLM typo like ``fraction=1.5`` fails early, not silently.
INVALID_SIGNAL_FRACTION = "invalid_signal_fraction"

#: ``Signal.target_exposure(target=...)`` outside ``[0, 1]``. The target is
#: the desired post-cycle long exposure as a fraction of account equity.
#: Rejected at compile (literal) and construction (computed) time — never
#: clamped — so a typo like ``1.2`` fails early instead of silently oversizing.
INVALID_TARGET_EXPOSURE = "invalid_target_exposure"

#: ``Signal.target_quantity(quantity=...)`` below ``0``. The quantity is the
#: desired post-cycle share inventory for the symbol. Rejected at compile
#: (literal) and construction (computed) time — never clamped — so a typo
#: like ``-100`` fails early instead of silently inverting the inventory.
INVALID_TARGET_QUANTITY = "invalid_target_quantity"

#: ``ctx.dp.get_bars(symbol=other)`` for a symbol not present in the
#: current cycle's ``informative_data`` declaration. Strategy must declare
#: cross-symbol dependencies up front so the worker can prefetch.
INFORMATIVE_DATA_NOT_DECLARED = "informative_data_not_declared"

#: ``ctx.dp.ticker(...)`` / ``orderbook(...)`` called during backtest.
LIVE_ONLY_METHOD = "live_only_method"

#: ``populate_indicators`` returned non-DataFrame or dropped required columns.
INVALID_POPULATE_INDICATORS_RETURN = "invalid_populate_indicators_return"

#: ``on_bar`` returned non-Signal object.
INVALID_ON_BAR_RETURN = "invalid_on_bar_return"

#: Industry resolution for ``$self.industry`` failed (no mapping found).
INDUSTRY_RESOLUTION_FAILED = "industry_resolution_failed"

#: ``informative_data`` returned a list containing non-DataRequest entries
#: or duplicate keys.
INVALID_INFORMATIVE_DATA_RETURN = "invalid_informative_data_return"


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class StrategyError(Exception):
    """Base for all SDK-raised exceptions carrying an ``error_code`` token.

    Always construct with a stable ``error_code`` from the module-level
    constants above. ``hint`` should be actionable, pointing to a skill
    section or a concrete code change (not just "check your input").
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        hint: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.context = dict(context) if context else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_type": type(self).__name__,
            "message": str(self),
            "hint": self.hint,
            **self.context,
        }


class StrategyCompileError(StrategyError):
    """Raised by :class:`StrategyCompiler` AST checks before execution."""


class StrategyValidationError(StrategyError):
    """Raised when a strategy method returned a value of the wrong shape."""


class DataAccessError(StrategyError):
    """Raised from ``ctx.dp.*`` when a runtime data access fails."""


class InformativeDataError(StrategyError):
    """Raised when ``informative_data`` declaration is malformed or undeclared
    data is accessed at runtime."""


__all__ = [
    # Compile-time error codes
    "DISALLOWED_IMPORT",
    "MISSING_ON_BAR",
    "MISSING_SIGNAL_TAG",
    "LOOKAHEAD_ACCESS",
    "POPULATE_CROSS_SYMBOL_ACCESS",
    "SILENT_EXCEPTION_SWALLOW",
    "SILENT_TYPE_COERCION",
    "UNKNOWN_DP_METHOD",
    "UNKNOWN_DATA_REQUEST_TYPE",
    "INVALID_INFORMATIVE_DECORATOR",
    "HISTORY_CHECK_LITERAL_DISALLOWED",
    "INVALID_CLASS_ATTRIBUTE",
    "CIRCULAR_INFORMATIVE_DEPENDENCY",
    # Runtime error codes
    "DATA_INSUFFICIENT",
    "INVALID_SYMBOL",
    "INVALID_ARGUMENT",
    "INVALID_TARGET_EXPOSURE",
    "INVALID_TARGET_QUANTITY",
    "INFORMATIVE_DATA_NOT_DECLARED",
    "LIVE_ONLY_METHOD",
    "INVALID_POPULATE_INDICATORS_RETURN",
    "INVALID_ON_BAR_RETURN",
    "INDUSTRY_RESOLUTION_FAILED",
    "INVALID_INFORMATIVE_DATA_RETURN",
    # Exception classes
    "StrategyError",
    "StrategyCompileError",
    "StrategyValidationError",
    "DataAccessError",
    "InformativeDataError",
]
