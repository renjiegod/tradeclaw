"""Strategy SDK discovery tools — let agents enumerate available APIs.

Three zero-side-effect tools that an agent should call **before** writing
a strategy module, so it doesn't hallucinate method names or factory
types that don't exist:

- :class:`ListDpMethodsTool` — every ``ctx.dp.*`` method with signature,
  description, error codes, and an example.
- :class:`ListDataRequestsTool` — every ``DataRequest.*`` factory with
  parameter schema.
- :class:`ListIndicatorsTool` — every registered indicator function in
  :mod:`doyoutrade.strategy_sdk.indicators`.

All three return ``status: ok`` with a structured payload — the
top-level surface (signatures, error_codes) is part of the public
contract enumerated by the StrategyCompiler's ``unknown_dp_method`` /
``unknown_data_request_type`` AST checks.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, get_type_hints

from doyoutrade.strategy_sdk import DataRequest, indicators
from doyoutrade.strategy_sdk.data_provider import DataProvider
from doyoutrade.strategy_sdk.data_requests import REGISTERED_REQUEST_TYPES
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)

logger = logging.getLogger(__name__)


# Registered DataProvider method names. Kept in sync with
# doyoutrade.strategy_runtime.compiler._REGISTERED_DP_METHODS — adding a
# new dp method requires updating both. Tests pin this invariant.
_DP_METHOD_NAMES: tuple[str, ...] = (
    "get_bars",
    "get_index_bars",
    "get_industry_members",
    "get_peer_bars",
    "get_fundamentals",
    "watchlist_symbols",
    "ticker",
    "orderbook",
)


# Indicators considered part of the public surface. Each must exist as a
# top-level function or class in doyoutrade.strategy_sdk.indicators.
_INDICATOR_NAMES: tuple[str, ...] = (
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "atr",
    "adx",
    "obv",
    # Momentum / overbought-oversold
    "kdj",
    "williams_r",
    "cci",
    "roc",
    "momentum",
    "mfi",
    "trix",
    # Volume / price-volume
    "vwap",
    "cmf",
    "ad",
    "volume_ratio",
    # Channel / volatility
    "keltner",
    "donchian",
    "stdev",
    "hist_volatility",
    # Trend (advanced)
    "wma",
    "dema",
    "kama",
    "supertrend",
    "psar",
    "ichimoku",
    "zigzag",
    # A-share regime (historical daily)
    "a_share_limit_pct",
    "limit_up_approx",
    "limit_down_approx",
    # Helpers
    "signal_from",
    "crossed_above",
    "crossed_below",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _method_signature(method: Any) -> str:
    try:
        sig = inspect.signature(method)
        return f"({', '.join(str(p) for p in sig.parameters.values() if p.name != 'self')})"
    except (TypeError, ValueError):
        return "(...)"


def _method_doc(method: Any) -> str:
    doc = inspect.getdoc(method) or ""
    return doc.splitlines()[0] if doc else ""


def _render_type(annotation: Any) -> str:
    """Render a type annotation as a short human-readable string.

    - ``pandas.Series`` → ``"pd.Series"`` so LLMs see the conventional
      import alias.
    - Classes with a ``__name__`` (e.g. ``MACDResult``) → the bare name.
    - Anything else → ``str(annotation)`` (typing aliases, etc.).
    """

    if annotation is None or annotation is type(None):  # noqa: E721
        return "None"
    module = getattr(annotation, "__module__", "")
    name = getattr(annotation, "__name__", None)
    if module == "pandas.core.series" or (
        isinstance(name, str) and name == "Series" and module.startswith("pandas")
    ):
        return "pd.Series"
    if isinstance(name, str) and name:
        return name
    return str(annotation)


def _is_named_tuple(annotation: Any) -> bool:
    """Best-effort detection of a ``typing.NamedTuple`` subclass."""

    return (
        isinstance(annotation, type)
        and issubclass(annotation, tuple)
        and hasattr(annotation, "_fields")
        and hasattr(annotation, "__annotations__")
    )


def _parse_field_docs(class_doc: str) -> dict[str, str]:
    """Best-effort field-doc extraction from a NamedTuple class docstring.

    Looks for lines shaped like ``- ``field``: text...`` (the convention
    used in :mod:`doyoutrade.strategy_sdk.indicators`). Returns ``{}`` if
    the docstring doesn't carry per-field annotations — we **never**
    fabricate per-field docs, the caller falls back to no ``doc`` key.
    """

    if not class_doc:
        return {}
    docs: dict[str, str] = {}
    for raw_line in class_doc.splitlines():
        line = raw_line.strip()
        if not line.startswith("- ``"):
            continue
        # Format: ``field``: description...
        try:
            after_dash = line[len("- ``"):]
            field, _, rest = after_dash.partition("``")
            if not field or not rest:
                continue
            # Strip leading ":" / whitespace from the description.
            text = rest.lstrip(": ").strip()
            if text:
                docs[field] = text
        except (ValueError, IndexError):
            continue
    return docs


def _describe_return_type(fn: Any) -> dict[str, Any] | None:
    """Describe the function's return annotation for LLM consumption.

    - NamedTuple subclass → ``{"type": "<Name>", "fields": [{name, type, doc?}]}``.
    - ``pd.Series`` (or any other concrete annotation) → ``{"type": "<rendered>"}``.
    - Annotation lookup failure → ``None`` (caller omits the field). We
      never fabricate field lists; emitting a wrong NamedTuple shape
      would re-introduce the very bug this helper exists to prevent.
    """

    try:
        hints = get_type_hints(fn)
    except Exception as exc:  # noqa: BLE001
        # ``get_type_hints`` can fail when annotations reference names not
        # in scope. Don't silently fabricate — log and skip the field.
        logger.warning(
            "list_indicators: get_type_hints failed for %s: %s: %s",
            getattr(fn, "__name__", repr(fn)),
            type(exc).__name__,
            exc,
        )
        return None
    annotation = hints.get("return")
    if annotation is None:
        return None
    if _is_named_tuple(annotation):
        try:
            field_hints = get_type_hints(annotation)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "list_indicators: get_type_hints failed for NamedTuple %s: %s: %s",
                getattr(annotation, "__name__", repr(annotation)),
                type(exc).__name__,
                exc,
            )
            return {"type": _render_type(annotation)}
        class_doc = inspect.getdoc(annotation) or ""
        field_docs = _parse_field_docs(class_doc)
        fields: list[dict[str, Any]] = []
        for field_name in annotation._fields:  # type: ignore[attr-defined]
            field_entry: dict[str, Any] = {
                "name": field_name,
                "type": _render_type(field_hints.get(field_name)),
            }
            doc_text = field_docs.get(field_name)
            if doc_text:
                field_entry["doc"] = doc_text
            fields.append(field_entry)
        return {"type": _render_type(annotation), "fields": fields}
    return {"type": _render_type(annotation)}


def _empty_request_payload(error: dict[str, Any], allowed: list[str]) -> ToolResult:
    text = format_error_text(
        "validation_error",
        str(error.get("message") or error.get("error") or "validation failed"),
        f"This tool takes no arguments; allowed top-level keys: {allowed!r}",
    )
    return ToolResult(text=text, is_error=True)


# ---------------------------------------------------------------------------
# ListDpMethodsTool
# ---------------------------------------------------------------------------


class ListDpMethodsTool(OperationHandler):
    name = "list_dp_methods"
    description = (
        "Enumerate every method available on ``ctx.dp`` (the strategy's "
        "data-access facade). Returns one entry per method with: name, "
        "signature, single-line description, error codes the method can "
        "raise, and a minimal usage example. Call this before drafting a "
        "strategy so you don't reference methods that don't exist — the "
        "StrategyCompiler's ``unknown_dp_method`` AST check rejects calls "
        "to anything not in this list."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            if contract.error_kind == "unknown_arguments":
                return ToolResult(
                    text=format_unknown_args(
                        list(contract.error.get("unknown", [])),
                        sorted(self._allowed_top_level_kwargs()) or ["(none)"],
                        dict(contract.error.get("suggested_path") or {}),
                    ),
                    is_error=True,
                )
            return _empty_request_payload(contract.error, sorted(self._allowed_top_level_kwargs()))

        methods: list[dict[str, Any]] = []
        for name in _DP_METHOD_NAMES:
            method = getattr(DataProvider, name, None)
            if method is None:
                continue
            methods.append(
                {
                    "name": name,
                    "signature": f"ctx.dp.{name}{_method_signature(method)}",
                    "doc": _method_doc(method),
                    "errors": _DP_METHOD_ERRORS.get(name, []),
                    "example": _DP_METHOD_EXAMPLES.get(name, ""),
                }
            )

        header = (
            f"ctx.dp exposes {len(methods)} method(s). Call any of these "
            "from on_bar / populate_indicators (except cross-symbol "
            "get_bars in populate_indicators — forbidden by "
            "populate_cross_symbol_access)."
        )
        payload = {
            "status": "ok",
            "tool": self.name,
            "count": len(methods),
            "methods": methods,
            "notes": [
                "All ctx.dp methods are sync from the strategy's POV.",
                "Each call emits an OTel span + structured debug event.",
                "Cross-symbol / cross-index access MUST also be declared in informative_data().",
            ],
        }
        return ToolResult(text=append_json_payload(header, payload))


_DP_METHOD_ERRORS: dict[str, list[str]] = {
    "get_bars": ["invalid_argument", "invalid_symbol", "data_insufficient", "informative_data_not_declared"],
    "get_index_bars": ["invalid_argument", "invalid_symbol", "data_insufficient", "informative_data_not_declared"],
    "get_industry_members": ["invalid_argument", "industry_resolution_failed"],
    "get_peer_bars": ["invalid_argument", "industry_resolution_failed", "data_insufficient"],
    "get_fundamentals": ["invalid_argument", "data_insufficient", "informative_data_not_declared"],
    "watchlist_symbols": ["invalid_argument"],
    "ticker": ["live_only_method"],
    "orderbook": ["live_only_method"],
}


_DP_METHOD_EXAMPLES: dict[str, str] = {
    "get_bars": 'df = ctx.dp.get_bars(symbol="600519.SH", window=30)',
    "get_index_bars": 'df = ctx.dp.get_index_bars("000300.SH", window=20)',
    "get_industry_members": 'peers = ctx.dp.get_industry_members(top_n=20)',
    "get_peer_bars": 'peer_bars = ctx.dp.get_peer_bars(window=10, top_n=20)',
    "get_fundamentals": 'fund = ctx.dp.get_fundamentals(fields=("pe", "pb"))',
    "watchlist_symbols": 'symbols = ctx.dp.watchlist_symbols(tag="核心池")',
    "ticker": 'tk = ctx.dp.ticker()  # live only',
    "orderbook": 'ob = ctx.dp.orderbook(depth=5)  # live only',
}


# ---------------------------------------------------------------------------
# ListDataRequestsTool
# ---------------------------------------------------------------------------


class ListDataRequestsTool(OperationHandler):
    name = "list_data_requests"
    description = (
        "Enumerate every DataRequest factory available for "
        "Strategy.informative_data(). Returns the factory name, signature, "
        "parameter description, and an example. Use this before drafting "
        "informative_data() — the StrategyCompiler's "
        "``unknown_data_request_type`` AST check rejects DataRequest.<name>() "
        "calls where <name> isn't in this list."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            if contract.error_kind == "unknown_arguments":
                return ToolResult(
                    text=format_unknown_args(
                        list(contract.error.get("unknown", [])),
                        sorted(self._allowed_top_level_kwargs()) or ["(none)"],
                        dict(contract.error.get("suggested_path") or {}),
                    ),
                    is_error=True,
                )
            return _empty_request_payload(contract.error, sorted(self._allowed_top_level_kwargs()))

        factories: list[dict[str, Any]] = []
        for name in sorted(REGISTERED_REQUEST_TYPES):
            factory = getattr(DataRequest, name, None)
            if factory is None:
                continue
            factories.append(
                {
                    "name": name,
                    "signature": f"DataRequest.{name}{_method_signature(factory)}",
                    "doc": _method_doc(factory) or _REQUEST_DOC.get(name, ""),
                    "returns": _REQUEST_RETURNS.get(name, ""),
                    "example": _REQUEST_EXAMPLES.get(name, ""),
                }
            )

        header = (
            f"DataRequest exposes {len(factories)} factory type(s). Use "
            "them from Strategy.informative_data() to declare cross-symbol "
            "/ cross-index / cross-section data the runner will prefetch."
        )
        payload = {
            "status": "ok",
            "tool": self.name,
            "count": len(factories),
            "factories": factories,
            "notes": [
                "Symbolic references supported: $self (current symbol), $self.industry (current industry).",
                "Every cross-symbol / cross-index reference must be declared here.",
                "Returned objects are immutable dataclasses with a cache_key() method.",
            ],
        }
        return ToolResult(text=append_json_payload(header, payload))


_REQUEST_DOC: dict[str, str] = {
    "bars": "Historical OHLCV bars for a specific symbol.",
    "index_bars": "Historical OHLCV bars for a market index / ETF.",
    "peers": "Top-N industry-peer OHLCV panel (defaults to $self.industry).",
    "cross_section": "Panel snapshot of selected fields across the universe.",
    "fundamentals": "Latest fundamental metrics for a symbol.",
}


_REQUEST_RETURNS: dict[str, str] = {
    "bars": "BarsRequest — runner prefetches into ctx.dp cache",
    "index_bars": "IndexBarsRequest",
    "peers": "PeersRequest",
    "cross_section": "CrossSectionRequest",
    "fundamentals": "FundamentalsRequest",
}


_REQUEST_EXAMPLES: dict[str, str] = {
    "bars": 'DataRequest.bars(symbol="600519.SH", window=30)',
    "index_bars": 'DataRequest.index_bars("000300.SH", window=30)',
    "peers": 'DataRequest.peers(window=10, top_n=20)',
    "cross_section": 'DataRequest.cross_section(fields=("market_cap", "turnover"))',
    "fundamentals": 'DataRequest.fundamentals(fields=("pe", "pb"))',
}


# ---------------------------------------------------------------------------
# ListIndicatorsTool
# ---------------------------------------------------------------------------


class ListIndicatorsTool(OperationHandler):
    name = "list_indicators"
    description = (
        "Enumerate technical indicators available in "
        "doyoutrade.strategy_sdk.indicators. Returns each indicator's "
        "signature and one-line doc. Use these inside populate_indicators "
        "rather than hand-rolling ewm / rolling chains — the indicators "
        "module is the single source of truth for MACD / RSI / ADX / "
        "Bollinger / ATR / SMA / EMA formulas."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            if contract.error_kind == "unknown_arguments":
                return ToolResult(
                    text=format_unknown_args(
                        list(contract.error.get("unknown", [])),
                        sorted(self._allowed_top_level_kwargs()) or ["(none)"],
                        dict(contract.error.get("suggested_path") or {}),
                    ),
                    is_error=True,
                )
            return _empty_request_payload(contract.error, sorted(self._allowed_top_level_kwargs()))

        entries: list[dict[str, Any]] = []
        for name in _INDICATOR_NAMES:
            fn = getattr(indicators, name, None)
            if fn is None:
                continue
            entry: dict[str, Any] = {
                "name": name,
                "signature": f"indicators.{name}{_method_signature(fn)}",
            }
            return_type = _describe_return_type(fn)
            if return_type is not None:
                entry["return_type"] = return_type
            entry["doc"] = _method_doc(fn)
            entries.append(entry)

        header = (
            f"{len(entries)} indicator(s) available. For multi-output "
            "indicators (macd, bollinger, adx), `return_type.fields` "
            "lists the attribute names on the returned NamedTuple — use "
            "those exact names to avoid AttributeError at smoke."
        )
        payload = {
            "status": "ok",
            "tool": self.name,
            "count": len(entries),
            "indicators": entries,
            "notes": [
                "All return pandas Series (or NamedTuple of Series for multi-output) aligned to input index.",
                "Warm-up bars produce NaN; gate iloc[-1] reads with pd.isna().",
                "Indicator periods are part of startup_history sizing — set startup_history >= longest rolling window.",
                "Multi-output indicators return a NamedTuple — read field names from return_type.fields (e.g. MACDResult.hist, NOT .histogram).",
            ],
        }
        return ToolResult(text=append_json_payload(header, payload))


__all__ = [
    "ListDataRequestsTool",
    "ListDpMethodsTool",
    "ListIndicatorsTool",
]
