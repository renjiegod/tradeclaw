"""StrategyCompiler — AST + runtime validation for Strategy source code.

The compiler is the single gatekeeper between agent-authored strategy
source and the runner. It performs three passes:

1. **Parse** — ``ast.parse`` to catch syntax errors with line numbers.
2. **AST checks** — structural validation that doesn't require executing
   the source. Each check raises with a stable :mod:`doyoutrade.strategy_sdk.errors`
   ``error_code`` token so skill documentation can cross-reference repair
   recipes by name. The full set of checks:

   - ``disallowed_import`` — only the whitelisted modules may be imported.
   - ``missing_on_bar`` — subclass must implement ``on_bar``.
   - ``missing_signal_tag`` — every ``Signal.buy()`` / ``.sell()`` /
     ``.target_exposure()`` / ``.target_quantity()`` call must pass a
     ``tag=`` keyword.
   - ``invalid_target_exposure`` — a literal target outside ``[0, 1]``.
   - ``invalid_target_quantity`` — a literal quantity below ``0``.
   - ``lookahead_access`` — ``df.iloc[i]`` with non-negative ``i`` or
     ``df.shift(-n)`` reads forward in time; the current bar is always
     ``df.iloc[-1]``.
   - ``populate_cross_symbol_access`` — ``ctx.dp.get_bars(symbol=<other>)``
     inside ``populate_indicators``. Cross-symbol access must go through
     ``informative_data`` + a separate populate pass.
   - ``silent_exception_swallow`` — ``except Exception: pass`` /
     ``except: pass`` / silent ``continue``. Violates CLAUDE.md's "错误
     可见性" rule.
   - ``silent_type_coercion`` — ``if not isinstance(x, T): x = default``
     patterns that hide schema violations.
   - ``unknown_dp_method`` — ``ctx.dp.<name>(...)`` where ``<name>`` is
     not a registered DataProvider method.
   - ``unknown_data_request_type`` — ``DataRequest.<name>(...)`` where
     ``<name>`` is not a registered factory.
   - ``invalid_class_attribute`` — wrong type / range for ``timeframe``,
     ``startup_history``, ``name``.
   - ``history_check_literal_disallowed`` — a ``rolling(N)`` / ``len(df)
     < N`` literal that exceeds ``startup_history`` (would silently fail
     to compute on smoke data).

3. **Exec** — compile + ``exec`` the AST in a constrained namespace, then
   verify the resulting class is a :class:`Strategy` subclass and
   implements ``on_bar``. Parameters / informative specs are extracted
   for the descriptor.

After ``validate_definition`` succeeds, :meth:`smoke_test` instantiates
the strategy and runs ``populate_indicators`` + ``on_bar`` against several
synthetic price regimes (monotone / flat / zigzag / step_up). This catches
NaN propagation, AttributeError from typos, and Signal-validation failures
before the strategy ever reaches a real cycle.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import logging
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)

from doyoutrade.strategy_sdk import (
    BarsRequest,
    BooleanParameter,
    CategoricalParameter,
    CrossSectionRequest,
    DataRequest,
    DecimalParameter,
    Direction,
    ExitReason,
    FundamentalsRequest,
    IndexBarsRequest,
    IntParameter,
    PeersRequest,
    Signal,
    Strategy,
    StrategyDescriptor,
    decimal_from_number,
    indicators,
    informative,
    informative_each,
    patterns,
)
from doyoutrade.strategy_sdk.data_requests import REGISTERED_REQUEST_TYPES
from doyoutrade.strategy_sdk.errors import (
    DISALLOWED_IMPORT,
    HISTORY_CHECK_LITERAL_DISALLOWED,
    INVALID_CLASS_ATTRIBUTE,
    INVALID_SIGNAL_FRACTION,
    INVALID_TARGET_EXPOSURE,
    INVALID_TARGET_QUANTITY,
    LOOKAHEAD_ACCESS,
    MISSING_ON_BAR,
    MISSING_SIGNAL_TAG,
    POPULATE_CROSS_SYMBOL_ACCESS,
    SILENT_EXCEPTION_SWALLOW,
    SILENT_TYPE_COERCION,
    UNKNOWN_DATA_REQUEST_TYPE,
    UNKNOWN_DP_METHOD,
)
from doyoutrade.strategy_sdk.informative import collect_informative_specs
from doyoutrade.strategy_sdk.parameters import collect_parameters


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledStrategyArtifact:
    """Successfully compiled strategy class + extracted metadata."""

    class_name: str
    qualified_name: str
    strategy_class: type
    descriptor: StrategyDescriptor


@dataclass(frozen=True)
class StrategyCompileResult:
    """Outcome of :meth:`StrategyCompiler.validate_definition`.

    ``success=True`` ⇒ ``artifact`` populated, ``errors`` empty.
    ``success=False`` ⇒ ``error_code`` is a stable token from
    ``strategy_sdk.errors``; ``validation_errors`` carries structured
    detail per offending location; ``repair_hints`` are operator-facing
    strings.

    Factory methods for directory-based compilation:
    - :meth:`failure` — build a failed result without a code_hash.
    - :meth:`ok_result` — build a successful result without a code_hash.

    Compatibility properties:
    - ``.ok`` — alias for ``success`` (frozen dataclasses block attribute
      writes, not reads; property access works normally).
    - ``.error_dicts`` — ``validation_errors`` with an ``error_code`` key
      added to each dict (using the ``type`` key as source).
    """

    success: bool
    code_hash: str
    artifact: CompiledStrategyArtifact | None = None
    errors: tuple[str, ...] = ()
    error_code: str | None = None
    validation_errors: tuple[dict[str, Any], ...] = ()
    repair_hints: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Alias for ``success``; used by ``validate_directory`` callers."""
        return self.success

    @property
    def error_dicts(self) -> tuple[dict[str, Any], ...]:
        """``validation_errors`` normalised to always have an ``error_code`` key.

        ``validate_definition`` stores violations under the ``type`` key;
        ``validate_directory`` callers (and the multi-file test suite) expect
        ``error_code``. This property adds ``error_code`` from ``type`` when
        the former is absent, making both call paths uniform.
        """
        out: list[dict[str, Any]] = []
        for d in self.validation_errors:
            entry = dict(d)
            if "error_code" not in entry:
                entry["error_code"] = entry.get("type", self.error_code or "unknown_error")
            out.append(entry)
        return tuple(out)

    @staticmethod
    def failure(
        *,
        error_code: str,
        errors: tuple[str, ...] | list[str] = (),
        error_dicts: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        repair_hints: tuple[str, ...] | list[str] = (),
    ) -> "StrategyCompileResult":
        """Build a failed :class:`StrategyCompileResult` for directory compilation.

        Each dict in ``error_dicts`` must contain either ``error_code`` or
        ``type``; the ``error_dicts`` property will normalise them. Using
        ``error_code`` directly is preferred for new code.
        """
        # Normalise: store under validation_errors with type=error_code for
        # compat with _result_from_violations callers; keep error_code key too.
        normalised: list[dict[str, Any]] = []
        for d in error_dicts:
            entry = dict(d)
            if "type" not in entry:
                entry["type"] = entry.get("error_code", error_code)
            if "error_code" not in entry:
                entry["error_code"] = entry.get("type", error_code)
            normalised.append(entry)
        return StrategyCompileResult(
            success=False,
            code_hash="",
            error_code=error_code,
            errors=tuple(errors),
            validation_errors=tuple(normalised),
            repair_hints=tuple(repair_hints),
        )

    @staticmethod
    def ok_result(
        *,
        artifact: "CompiledStrategyArtifact",
    ) -> "StrategyCompileResult":
        """Build a successful :class:`StrategyCompileResult` for directory compilation."""
        return StrategyCompileResult(
            success=True,
            code_hash="",
            artifact=artifact,
        )


@dataclass(frozen=True)
class StrategySmokeResult:
    """Outcome of :meth:`StrategyCompiler.smoke_test`.

    Runs ``populate_indicators`` + ``on_bar`` against synthetic regimes
    with zero side effects (no debug events, no DB writes). Failure codes:

    - ``runtime_smoke_failed`` — ``__init__`` / ``populate_indicators`` /
      ``on_bar`` raised. ``error_type`` / ``error_message`` /
      ``traceback_excerpt`` populated.
    - ``smoke_output_invalid`` — ``populate_indicators`` returned non-DF,
      or ``on_bar`` returned non-Signal.
    """

    success: bool
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    traceback_excerpt: str | None = None
    validation_errors: tuple[dict[str, Any], ...] = ()
    repair_hints: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# AST whitelists
# ---------------------------------------------------------------------------


_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "__future__",  # syntax hints only, no runtime effect
        "decimal",
        "math",
        "numpy",
        "pandas",
        "doyoutrade.strategy_sdk",
        "typing",  # type hints only (ClassVar etc.)
    }
)


_RESTRICTED_BUILTINS = MappingProxyType(
    {
        name: getattr(builtins, name)
        for name in (
            "__build_class__",
            "abs",
            "all",
            "any",
            "bool",
            "dict",
            "enumerate",
            "filter",
            "float",
            "frozenset",
            "hasattr",
            "int",
            "isinstance",
            "issubclass",
            "len",
            "list",
            "map",
            "max",
            "min",
            "next",
            "pow",
            "range",
            "repr",
            "reversed",
            "round",
            "set",
            "sorted",
            "str",
            "sum",
            "tuple",
            "zip",
        )
    }
)


_SDK_NAMESPACE = MappingProxyType(
    {
        "BarsRequest": BarsRequest,
        "BooleanParameter": BooleanParameter,
        "CategoricalParameter": CategoricalParameter,
        "CrossSectionRequest": CrossSectionRequest,
        "DataRequest": DataRequest,
        "Decimal": Decimal,
        "DecimalParameter": DecimalParameter,
        "Direction": Direction,
        "ExitReason": ExitReason,
        "FundamentalsRequest": FundamentalsRequest,
        "IndexBarsRequest": IndexBarsRequest,
        "IntParameter": IntParameter,
        "PeersRequest": PeersRequest,
        "Signal": Signal,
        "Strategy": Strategy,
        "StrategyDescriptor": StrategyDescriptor,
        "decimal_from_number": decimal_from_number,
        "indicators": indicators,
        "informative": informative,
        "informative_each": informative_each,
        "patterns": patterns,
    }
)


# Registered DataProvider method names — kept in sync with
# doyoutrade.strategy_sdk.data_provider.DataProvider. Updating one
# without the other is a contract bug; the test_strategy_compiler suite
# pins this invariant.
_REGISTERED_DP_METHODS: frozenset[str] = frozenset(
    {
        "get_bars",
        "get_index_bars",
        "get_industry_members",
        "get_peer_bars",
        "get_fundamentals",
        "watchlist_symbols",
        "ticker",
        "orderbook",
    }
)


# Canonical bar intervals the data layer can actually serve. These MUST stay in
# sync with the providers' ``ProviderCapabilities.supported_intervals``
# (doyoutrade/data/*_provider.py, qmt_proxy.py) — declaring a timeframe the data
# layer cannot fetch is a compile-time drift bug that would otherwise only
# surface at runtime as ``data_insufficient``. Hourly is ``60m`` (not ``1h``)
# and monthly is ``1mo`` (not ``1M``); ``4h`` is served by no provider.
_VALID_TIMEFRAMES: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"}
)


# ---------------------------------------------------------------------------
# AST visitors
# ---------------------------------------------------------------------------


@dataclass
class _ASTViolation:
    """One AST-level violation. Mirrors :class:`StrategyError.to_dict`."""

    error_code: str
    message: str
    lineno: int
    col_offset: int
    hint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class _StrategyASTVisitor(ast.NodeVisitor):
    """Walks a Strategy module's AST collecting violations.

    Tracks the current class / method context so checks that only apply
    inside specific scopes (e.g. ``populate_cross_symbol_access`` only
    fires inside ``populate_indicators``) can scope correctly.
    """

    def __init__(
        self,
        *,
        strategy_startup_history: int | None = None,
        extra_allowed_modules: frozenset[str] | None = None,
    ):
        self.violations: list[_ASTViolation] = []
        self._strategy_startup_history = strategy_startup_history
        # extra_allowed_modules: local module names discovered from the
        # code_root directory tree (e.g. "helpers", "indicators.ma").
        # Used by validate_directory so that cross-file imports are accepted.
        self._extra_allowed_modules: frozenset[str] = extra_allowed_modules or frozenset()
        self._class_stack: list[str] = []
        self._function_stack: list[str] = []

    # ----- Imports -----

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not self._is_allowed_import(alias.name, self._extra_allowed_modules):
                self.violations.append(
                    _ASTViolation(
                        error_code=DISALLOWED_IMPORT,
                        message=f"disallowed import: {alias.name}",
                        lineno=node.lineno,
                        col_offset=node.col_offset,
                        hint=(
                            "Strategies may only import: decimal / math / "
                            "numpy / pandas / doyoutrade.strategy_sdk. All "
                            "data access goes through ctx.dp."
                        ),
                        extra={"module": alias.name},
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not self._is_allowed_import(module, self._extra_allowed_modules):
            self.violations.append(
                _ASTViolation(
                    error_code=DISALLOWED_IMPORT,
                    message=f"disallowed import: from {module}",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    extra={"module": module},
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_allowed_import(
        module: str,
        extra_allowed: frozenset[str] | None = None,
    ) -> bool:
        if not module:
            return False
        for allowed in _ALLOWED_IMPORTS:
            if module == allowed or module.startswith(allowed + "."):
                return True
        if extra_allowed:
            # Allow local module names and their sub-modules
            # e.g. extra_allowed = {"helpers", "indicators.ma"}
            root = module.split(".", 1)[0]
            if module in extra_allowed or root in extra_allowed:
                return True
        return False

    # ----- Class / function context tracking -----

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Strategies must not be async; flag and continue.
        if self._class_stack:
            self.violations.append(
                _ASTViolation(
                    error_code=DISALLOWED_IMPORT,
                    message=f"async def {node.name} inside Strategy is not allowed",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint="Strategy methods must be synchronous; the runner handles async I/O.",
                )
            )
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    # ----- except handlers (silent_exception_swallow) -----

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # Allowed: except SomeSpecificError: <body that logs+re-emits>
        # Forbidden: bare except: / except Exception: with pass / silent continue.
        if self._is_broad_except(node) and self._is_silent_body(node.body):
            self.violations.append(
                _ASTViolation(
                    error_code=SILENT_EXCEPTION_SWALLOW,
                    message=(
                        f"silent broad except handler at line {node.lineno} "
                        "swallows errors without logging or re-raising"
                    ),
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "Either narrow the exception type and add a "
                        "logger.warning + structured debug event, or remove "
                        "the try/except and let the failure propagate. "
                        "See CLAUDE.md 错误可见性 / 静默吞 bug 禁令."
                    ),
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_broad_except(node: ast.ExceptHandler) -> bool:
        if node.type is None:
            return True
        if isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            return True
        return False

    @staticmethod
    def _is_silent_body(body: list[ast.stmt]) -> bool:
        # Single Pass / Continue / single bare Return None.
        if len(body) != 1:
            # Multi-statement bodies are usually doing something — only
            # flag the truly silent single-statement case.
            return False
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Continue):
            return True
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True
        return False

    # ----- if not isinstance(x, T): x = default (silent_type_coercion) -----

    def visit_If(self, node: ast.If) -> None:
        if self._is_silent_isinstance_fallback(node):
            self.violations.append(
                _ASTViolation(
                    error_code=SILENT_TYPE_COERCION,
                    message=(
                        "silent type coercion: 'if not isinstance(x, T): x = default' "
                        "hides schema violations"
                    ),
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "Raise ValueError / TypeError with the actual type "
                        "and value instead of falling back to a default. "
                        "See CLAUDE.md 错误可见性."
                    ),
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_silent_isinstance_fallback(node: ast.If) -> bool:
        # Match: if not isinstance(X, T): X = <const>
        test = node.test
        if not (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Call)
            and isinstance(test.operand.func, ast.Name)
            and test.operand.func.id == "isinstance"
            and len(test.operand.args) == 2
            and isinstance(test.operand.args[0], ast.Name)
        ):
            return False
        var_name = test.operand.args[0].id
        if len(node.body) != 1:
            return False
        body = node.body[0]
        if not isinstance(body, ast.Assign):
            return False
        if len(body.targets) != 1 or not isinstance(body.targets[0], ast.Name):
            return False
        return body.targets[0].id == var_name

    # ----- Subscripts / shifts (lookahead_access) -----

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_lookahead_iloc(node):
            self.violations.append(
                _ASTViolation(
                    error_code=LOOKAHEAD_ACCESS,
                    message=(
                        "lookahead access: df.iloc[i] with non-negative i "
                        "reads forward in time"
                    ),
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint="Use df.iloc[-1] (current bar) / df.iloc[-2] (prior bar) etc.",
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_lookahead_iloc(node: ast.Subscript) -> bool:
        # Match: <anything>.iloc[<const non-negative int>]
        if not (
            isinstance(node.value, ast.Attribute) and node.value.attr == "iloc"
        ):
            return False
        idx = node.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
            return idx.value >= 0
        # df.iloc[i:j]: slices are inspected by hand; non-trivial,
        # skip — most are fine because they go [-N:].
        return False

    def visit_Call(self, node: ast.Call) -> None:
        # df.shift(-n) reads forward.
        if self._is_lookahead_shift(node):
            self.violations.append(
                _ASTViolation(
                    error_code=LOOKAHEAD_ACCESS,
                    message="lookahead access: df.shift(-n) shifts data from the future",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint="Use df.shift(N) with N>=1 to look at past bars; never negative.",
                )
            )

        # Signal.buy / Signal.sell / Signal.target_exposure / target_quantity
        # must have tag= kwarg.
        if self._is_missing_signal_tag(node):
            self.violations.append(
                _ASTViolation(
                    error_code=MISSING_SIGNAL_TAG,
                    message="Signal.buy() / Signal.sell() / Signal.target_exposure() / Signal.target_quantity() must include tag=",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "Add tag='your_factor_name' so trade_fills.entry_tag "
                        "and debug events can attribute the decision."
                    ),
                )
            )

        # Signal.sell(fraction=<literal>) must be in (0, 1].
        bad_fraction = self._invalid_signal_fraction(node)
        if bad_fraction is not None:
            self.violations.append(
                _ASTViolation(
                    error_code=INVALID_SIGNAL_FRACTION,
                    message=f"Signal.sell(fraction={bad_fraction!r}) must be in (0, 1]",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "fraction is the portion of the held position to sell "
                        "(1.0 = full exit, the default; 0.5 = half). Use a value "
                        "in (0, 1]; reject 0, >1, and negatives."
                    ),
                )
            )

        bad_target = self._invalid_target_exposure(node)
        if bad_target is not None:
            self.violations.append(
                _ASTViolation(
                    error_code=INVALID_TARGET_EXPOSURE,
                    message=f"Signal.target_exposure(target={bad_target!r}) must be in [0, 1]",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "target is the desired post-cycle long exposure as a fraction "
                        "of account equity: 0=flat, 0.5=half allocated, 1=fully allocated."
                    ),
                )
            )

        bad_quantity = self._invalid_target_quantity(node)
        if bad_quantity is not None:
            self.violations.append(
                _ASTViolation(
                    error_code=INVALID_TARGET_QUANTITY,
                    message=f"Signal.target_quantity(quantity={bad_quantity!r}) must be >= 0",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "quantity is the desired post-cycle share inventory. "
                        "Use 0 for flat, or a non-negative share count such as "
                        "100 / 200 / 300 for strict inventory grids."
                    ),
                )
            )

        # ctx.dp.<name>() — must be registered method.
        unknown_dp = self._unknown_dp_method(node)
        if unknown_dp is not None:
            self.violations.append(
                _ASTViolation(
                    error_code=UNKNOWN_DP_METHOD,
                    message=f"ctx.dp.{unknown_dp}() is not a registered method",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "Call list_dp_methods to discover available methods. "
                        f"Known: {sorted(_REGISTERED_DP_METHODS)}."
                    ),
                    extra={"method": unknown_dp},
                )
            )

        # DataRequest.<name>() — must be registered factory.
        unknown_dr = self._unknown_data_request_type(node)
        if unknown_dr is not None:
            self.violations.append(
                _ASTViolation(
                    error_code=UNKNOWN_DATA_REQUEST_TYPE,
                    message=f"DataRequest.{unknown_dr}() is not a registered factory",
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "Call list_data_requests to discover factories. "
                        f"Known: {sorted(REGISTERED_REQUEST_TYPES)}."
                    ),
                    extra={"factory": unknown_dr},
                )
            )

        # In populate_indicators: ctx.dp.get_bars(symbol=<not $self>) is forbidden.
        if self._is_populate_cross_symbol(node):
            self.violations.append(
                _ASTViolation(
                    error_code=POPULATE_CROSS_SYMBOL_ACCESS,
                    message=(
                        "populate_indicators cannot read cross-symbol bars; "
                        "declare them in informative_data and use them in on_bar"
                    ),
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    hint=(
                        "populate_indicators is per-symbol vectorized. Move "
                        "cross-symbol reads to on_bar, or use @informative "
                        "(symbol=...) to compute on another symbol."
                    ),
                )
            )

        # rolling(N) / len(df) < N where N > startup_history.
        history_violation = self._history_check_literal(node)
        if history_violation is not None:
            self.violations.append(history_violation)

        self.generic_visit(node)

    @staticmethod
    def _is_lookahead_shift(node: ast.Call) -> bool:
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "shift"):
            return False
        if not node.args:
            return False
        arg = node.args[0]
        if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
            # shift(-N)
            return True
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            return arg.value < 0
        return False

    @staticmethod
    def _is_missing_signal_tag(node: ast.Call) -> bool:
        # Match: Signal.buy(...) / Signal.sell(...) / Signal.target_exposure(...)
        # / Signal.target_quantity(...)
        # without tag= kwarg.
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Signal"
            and node.func.attr in ("buy", "sell", "target_exposure", "target_quantity")
        ):
            return False
        for kw in node.keywords:
            if kw.arg == "tag":
                # Tag with a string literal — verify non-empty; tag with
                # a name/expr — accept (runtime validation will catch).
                if isinstance(kw.value, ast.Constant) and not (
                    isinstance(kw.value.value, str) and kw.value.value.strip()
                ):
                    return True
                return False
        return True

    @staticmethod
    def _invalid_signal_fraction(node: ast.Call) -> float | int | None:
        # Match: Signal.sell(fraction=<numeric literal>) outside (0, 1].
        # Computed (non-literal) fractions are accepted here and validated at
        # construction time by Signal.sell (_validate_fraction). Returns the
        # offending literal value, else None.
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Signal"
            and node.func.attr == "sell"
        ):
            return None
        for kw in node.keywords:
            if kw.arg != "fraction":
                continue
            val: float | int | None = None
            if isinstance(kw.value, ast.Constant) and isinstance(
                kw.value.value, (int, float)
            ) and not isinstance(kw.value.value, bool):
                val = kw.value.value
            elif (
                isinstance(kw.value, ast.UnaryOp)
                and isinstance(kw.value.op, ast.USub)
                and isinstance(kw.value.operand, ast.Constant)
                and isinstance(kw.value.operand.value, (int, float))
                and not isinstance(kw.value.operand.value, bool)
            ):
                val = -kw.value.operand.value
            if val is None:
                return None  # computed expression — defer to runtime validation
            if not (val > 0) or val > 1:
                return val
            return None
        return None

    @staticmethod
    def _invalid_target_exposure(node: ast.Call) -> float | int | None:
        # Match: Signal.target_exposure(target=<numeric literal>) outside [0, 1].
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Signal"
            and node.func.attr == "target_exposure"
        ):
            return None
        target_value: ast.expr | None = None
        for kw in node.keywords:
            if kw.arg == "target":
                target_value = kw.value
                break
        if target_value is None and node.args:
            target_value = node.args[0]
        if not isinstance(target_value, ast.Constant) or not isinstance(
            target_value.value, (int, float)
        ):
            return None
        value = target_value.value
        if value < 0 or value > 1:
            return value
        return None

    @staticmethod
    def _invalid_target_quantity(node: ast.Call) -> float | int | None:
        # Match: Signal.target_quantity(quantity=<numeric literal>) below 0.
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Signal"
            and node.func.attr == "target_quantity"
        ):
            return None
        quantity_value: ast.expr | None = None
        for kw in node.keywords:
            if kw.arg == "quantity":
                quantity_value = kw.value
                break
        if quantity_value is None and node.args:
            quantity_value = node.args[0]
        if isinstance(quantity_value, ast.UnaryOp) and isinstance(quantity_value.op, ast.USub):
            operand = quantity_value.operand
            if isinstance(operand, ast.Constant) and isinstance(operand.value, (int, float)):
                return -operand.value
            return None
        if not isinstance(quantity_value, ast.Constant) or not isinstance(
            quantity_value.value, (int, float)
        ):
            return None
        value = quantity_value.value
        if value < 0:
            return value
        return None

    @staticmethod
    def _unknown_dp_method(node: ast.Call) -> str | None:
        # Match: ctx.dp.<name>(...). Returns <name> if unregistered.
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "dp"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "ctx"
        ):
            return None
        method = func.attr
        if method in _REGISTERED_DP_METHODS:
            return None
        return method

    @staticmethod
    def _unknown_data_request_type(node: ast.Call) -> str | None:
        # Match: DataRequest.<name>(...). Returns <name> if unregistered.
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "DataRequest"
        ):
            return None
        factory = func.attr
        if factory in REGISTERED_REQUEST_TYPES:
            return None
        return factory

    def _is_populate_cross_symbol(self, node: ast.Call) -> bool:
        # Only fires inside ``populate_indicators`` or @informative-decorated
        # methods that have no symbol arg (i.e., current-symbol only).
        if not self._function_stack:
            return False
        current_fn = self._function_stack[-1]
        if current_fn != "populate_indicators":
            return False
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "get_bars"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "dp"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "ctx"
        ):
            return False
        # ctx.dp.get_bars(symbol=X) where X is not $self / None.
        for kw in node.keywords:
            if kw.arg == "symbol":
                if isinstance(kw.value, ast.Constant):
                    val = kw.value.value
                    if val is None or val == "$self":
                        return False
                    return True
                # symbol=<some expression> — flag conservatively as cross.
                return True
        # No symbol kwarg — defaults to $self, allowed.
        return False

    def _history_check_literal(self, node: ast.Call) -> _ASTViolation | None:
        # Match rolling(N) where N (constant int) > startup_history.
        if self._strategy_startup_history is None:
            return None
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "rolling"):
            return None
        if not node.args:
            return None
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, int)):
            return None
        window = arg.value
        if window <= self._strategy_startup_history:
            return None
        return _ASTViolation(
            error_code=HISTORY_CHECK_LITERAL_DISALLOWED,
            message=(
                f"rolling({window}) exceeds startup_history="
                f"{self._strategy_startup_history}; the smoke data will be "
                "too short and indicators will return NaN."
            ),
            lineno=node.lineno,
            col_offset=node.col_offset,
            hint=(
                f"Raise startup_history to at least {window} or use a "
                "smaller rolling window."
            ),
            extra={
                "rolling_window": window,
                "startup_history": self._strategy_startup_history,
            },
        )


# ---------------------------------------------------------------------------
# StrategyCompiler
# ---------------------------------------------------------------------------


def _module_is_under(mod: Any, resolved_root: Path) -> bool:
    """Return True if ``mod.__file__`` resolves to a path inside ``resolved_root``.

    Uses ``Path.resolve()`` on the module's ``__file__`` so that macOS
    symlinks (``/var`` → ``/private/var``) don't cause the comparison to
    fail silently.
    """
    file_attr = getattr(mod, "__file__", None)
    if not file_attr:
        return False
    try:
        mod_path = Path(file_attr).resolve()
        mod_path.relative_to(resolved_root)
        return True
    except (ValueError, OSError):
        return False


class StrategyCompiler:
    """The single entry point for validating + smoke-testing Strategy source.

    Stateless — safe to instantiate per request. Two main methods:

    - :meth:`validate_definition` — parse + AST + exec + class validation.
    - :meth:`smoke_test` — instantiate and run synthetic-data cycles.
    """

    def validate_definition(
        self,
        source_code: str,
        class_name: str,
        *,
        strict_authoring: bool = False,
    ) -> StrategyCompileResult:
        """Parse, AST-check, exec, and verify the strategy class.

        ``strict_authoring=True`` is for authoring tools
        (``validate_strategy_draft``, registry write paths). Runtime load
        paths leave it default. Currently there are no strict-only checks
        for the new Strategy API, but the parameter is reserved for
        symmetry with the legacy compiler signature.
        """

        _ = strict_authoring
        code_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()[:16]
        try:
            tree = ast.parse(source_code, filename="<strategy_definition>", mode="exec")
        except SyntaxError as exc:
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code="syntax_error",
                errors=(f"invalid python source: {exc.msg}",),
                validation_errors=(
                    {
                        "type": "syntax_error",
                        "message": exc.msg,
                        "lineno": exc.lineno,
                        "offset": exc.offset,
                    },
                ),
                repair_hints=("Fix the Python syntax error and retry.",),
            )

        # Extract startup_history (literal class attribute) for the AST
        # visitor's history-check pass. Parsed from the AST so we don't
        # depend on exec succeeding.
        startup_history = _extract_class_int_attr(tree, class_name, "startup_history")

        # AST pass — collects all violations rather than short-circuiting.
        visitor = _StrategyASTVisitor(strategy_startup_history=startup_history)
        visitor.visit(tree)
        if visitor.violations:
            return _result_from_violations(code_hash, visitor.violations)

        # Exec in a constrained namespace.
        try:
            compiled = compile(tree, "<strategy_definition>", "exec")
            namespace = self._build_namespace()
            exec(compiled, namespace, namespace)
        except Exception as exc:  # noqa: BLE001
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code="compile_runtime_error",
                errors=(f"failed to compile strategy: {exc}",),
                validation_errors=(
                    {"type": "compile_runtime_error", "message": str(exc)},
                ),
                repair_hints=(
                    "Keep the strategy self-contained within the SDK surface.",
                ),
            )

        strategy_class = namespace.get(class_name)
        if strategy_class is None:
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code="missing_required_class",
                errors=(f"compiled source did not define class '{class_name}'",),
                validation_errors=(
                    {"type": "missing_required_class", "expected": class_name},
                ),
                repair_hints=(
                    f"Define a class named {class_name} inheriting Strategy.",
                ),
            )
        if not isinstance(strategy_class, type):
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code="not_a_class_definition",
                errors=(f"'{class_name}' is not a class definition",),
                validation_errors=(
                    {"type": "not_a_class_definition", "expected": class_name},
                ),
            )
        if not issubclass(strategy_class, Strategy):
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code="invalid_base_class",
                errors=(f"class '{class_name}' must inherit from Strategy",),
                validation_errors=(
                    {
                        "type": "invalid_base_class",
                        "expected": "Strategy",
                        "found": tuple(b.__name__ for b in strategy_class.__bases__),
                    },
                ),
                repair_hints=(
                    f"class {class_name}(Strategy): ...",
                ),
            )

        # on_bar must be implemented (override of abstract).
        on_bar = strategy_class.__dict__.get("on_bar")
        on_bar_inherited = on_bar is None
        for base in strategy_class.__mro__[1:]:
            if base is Strategy:
                break
            if "on_bar" in base.__dict__:
                on_bar_inherited = False
                break
        if on_bar is None and on_bar_inherited:
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code=MISSING_ON_BAR,
                errors=(f"{class_name} must implement on_bar",),
                validation_errors=(
                    {"type": MISSING_ON_BAR, "class_name": class_name},
                ),
                repair_hints=(
                    "Define on_bar(self, df, ctx) -> Signal that reads "
                    "df.iloc[-1] and returns Signal.buy(tag=...) / "
                    "Signal.sell(tag=...) / Signal.hold().",
                ),
            )

        # Class attribute type checks.
        attr_violation = _check_class_attributes(strategy_class)
        if attr_violation is not None:
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code=attr_violation.error_code,
                errors=(attr_violation.message,),
                validation_errors=(
                    {
                        "type": attr_violation.error_code,
                        "class_name": class_name,
                        **attr_violation.extra,
                    },
                ),
                repair_hints=(attr_violation.hint,) if attr_violation.hint else (),
            )

        # Parse ``# @param`` annotation comments out of the raw source and
        # inject any not already declared as class attributes. Annotation
        # form is a convenience for agents writing flat scripts; class
        # attributes (IntParameter(...)) remain authoritative when both
        # are present for the same name. Errors during parsing surface
        # as compile failures with the parser's stable error_code.
        try:
            from doyoutrade.strategy_sdk.parameter_annotations import (
                apply_parameter_annotations,
            )
            apply_parameter_annotations(strategy_class, source_code)
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "error_code", None) or "compile_runtime_error"
            return StrategyCompileResult(
                success=False,
                code_hash=code_hash,
                error_code=error_code,
                errors=(str(exc),),
                validation_errors=(
                    {"type": error_code, "message": str(exc)},
                ),
                repair_hints=(
                    getattr(exc, "hint", "")
                    or "Fix the @param annotation syntax; see strategy-definition-authoring skill.",
                ),
            )

        descriptor = self._describe_strategy(strategy_class)
        artifact = CompiledStrategyArtifact(
            class_name=class_name,
            qualified_name=f"{strategy_class.__module__}.{strategy_class.__qualname__}",
            strategy_class=strategy_class,
            descriptor=descriptor,
        )
        return StrategyCompileResult(
            success=True,
            code_hash=code_hash,
            artifact=artifact,
        )

    def _build_namespace(self) -> dict[str, Any]:
        restricted = dict(_RESTRICTED_BUILTINS)
        restricted["__import__"] = self._safe_import
        namespace: dict[str, Any] = {
            "__builtins__": restricted,
            "__name__": "doyoutrade.strategy_runtime.compiled",
            "__package__": "doyoutrade.strategy_runtime",
        }
        namespace.update(_SDK_NAMESPACE)
        return namespace

    @staticmethod
    def _safe_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level != 0:
            raise ImportError("relative imports are not allowed")
        root = name.split(".", 1)[0]
        if name not in _ALLOWED_IMPORTS and root not in _ALLOWED_IMPORTS:
            raise ImportError(f"disallowed import: {name}")
        return builtins.__import__(name, globals, locals, fromlist, level)

    def _describe_strategy(self, strategy_class: type) -> StrategyDescriptor:
        params = collect_parameters(strategy_class)
        informative_specs = collect_informative_specs(strategy_class)
        return StrategyDescriptor(
            name=getattr(strategy_class, "name", "") or strategy_class.__name__,
            description=(strategy_class.__doc__ or "").strip(),
            parameter_schema={
                name: descriptor.search_space()
                for name, descriptor in params.items()
            },
            capabilities={
                "timeframe": str(getattr(strategy_class, "timeframe", "1d")),
                "startup_history": int(getattr(strategy_class, "startup_history", 30)),
                "informative_specs": [
                    {
                        "method": s.method_name,
                        "timeframe": s.timeframe,
                        "symbol": s.symbol,
                        "each": s.each,
                        "column_suffix": s.column_suffix,
                    }
                    for s in informative_specs
                ],
            },
        )

    # ----- Directory-based compilation -----

    def validate_directory(
        self,
        code_root: "Path",
        *,
        strategy_class_name: str = "Strategy",
    ) -> StrategyCompileResult:
        """Validate every ``.py`` under ``code_root`` then exec the entry file.

        The AST pass walks each module so helpers cannot smuggle
        ``disallowed_import`` / lookahead / ``history_check_literal_disallowed`` /
        silent-except violations.

        Helper module isolation strategy: before importing, the code_root is
        inserted at ``sys.path[0]``; after exec we pop it and remove any
        ``sys.modules`` entries whose ``__file__`` resolves under code_root.
        A per-call uuid prefix in the module name prevents ``sys.modules``
        cache hits between successive calls to ``validate_directory``.
        """
        import importlib.util
        import sys
        from uuid import uuid4

        code_root = Path(code_root)
        entry = code_root / "strategy.py"
        if not entry.is_file():
            return StrategyCompileResult.failure(
                error_code="entry_file_missing",
                errors=("strategy.py must exist at the code_root",),
                error_dicts=(
                    {
                        "error_code": "entry_file_missing",
                        "type": "entry_file_missing",
                        "message": "strategy.py is required as the entry file",
                    },
                ),
            )

        # Discover all local module names so that cross-file imports like
        # ``from helpers import sma`` are permitted by the AST visitor.
        all_py = sorted(code_root.rglob("*.py"))
        local_modules: set[str] = set()
        for p in all_py:
            rel = p.relative_to(code_root)
            # "helpers.py" -> "helpers"; "indicators/ma.py" -> "indicators.ma"
            parts = list(rel.parts)
            if parts[-1].endswith(".py"):
                parts[-1] = parts[-1][:-3]
            module_dotted = ".".join(parts)
            local_modules.add(module_dotted)
            # Also allow the root package name ("indicators" for "indicators/ma.py")
            if "." in module_dotted:
                local_modules.add(module_dotted.split(".")[0])
        extra_allowed = frozenset(local_modules)

        # Stage 1: AST pass over all files (local imports permitted).
        ast_violations: list[dict[str, Any]] = []
        for path in all_py:
            source = path.read_text()
            tree = self._parse_or_collect(path, source, ast_violations, code_root=code_root)
            if tree is None:
                continue
            visitor = _StrategyASTVisitor(
                strategy_startup_history=None,  # resolved after exec
                extra_allowed_modules=extra_allowed,
            )
            visitor.visit(tree)
            for v in visitor.violations:
                d: dict[str, Any] = {
                    "error_code": v.error_code,
                    "type": v.error_code,
                    "message": v.message,
                    "lineno": v.lineno,
                    "col_offset": v.col_offset,
                    "path": str(path.relative_to(code_root)),
                }
                if v.hint:
                    d["hint"] = v.hint
                d.update(v.extra)
                ast_violations.append(d)

        if ast_violations:
            return StrategyCompileResult.failure(
                error_code=ast_violations[0]["error_code"],
                errors=tuple(v["message"] for v in ast_violations),
                error_dicts=tuple(ast_violations),
            )

        # Stage 2: exec the entry file with code_root on sys.path so local
        # helpers resolve. Use a uuid-prefixed module name to guarantee no
        # sys.modules cache collision between successive calls.
        call_token = uuid4().hex[:8]
        module_name = f"_doyoutrade_strategy_{call_token}_{code_root.name}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            entry,
            submodule_search_locations=[str(code_root)],
        )
        if spec is None or spec.loader is None:
            return StrategyCompileResult.failure(
                error_code="compile_runtime_error",
                errors=("could not build import spec for entry file",),
                error_dicts=(
                    {
                        "error_code": "compile_runtime_error",
                        "type": "compile_runtime_error",
                        "message": "spec_from_file_location returned None",
                    },
                ),
            )
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(code_root))
        # Suppress __pycache__/*.pyc generation: importing the strategy here
        # would otherwise drop CPython bytecode into the versioned code_root,
        # which is author source only. Stale bytecode there leaks into the
        # source viewer (binary decoded as UTF-8 → 乱码) and perturbs the
        # content hash. Restored in the finally so we never mutate the global
        # flag for unrelated imports.
        prev_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            msg = str(exc)
            return StrategyCompileResult.failure(
                error_code="compile_runtime_error",
                errors=(f"failed to load strategy module: {msg}",),
                error_dicts=(
                    {
                        "error_code": "compile_runtime_error",
                        "type": "compile_runtime_error",
                        "message": msg,
                    },
                ),
            )
        finally:
            sys.dont_write_bytecode = prev_dont_write_bytecode
            if sys.path and sys.path[0] == str(code_root):
                sys.path.pop(0)
            # Clean up any modules loaded from code_root so subsequent
            # validate_directory calls on different dirs don't get stale cache.
            # Use Path.resolve() on each module's __file__ so that macOS
            # symlinks (/var -> /private/var) don't cause the startswith
            # comparison to silently miss cached modules.
            code_root_resolved = code_root.resolve()
            to_remove = [
                name
                for name, mod in list(sys.modules.items())
                if (
                    name.startswith(f"_doyoutrade_strategy_{call_token}")
                    or _module_is_under(mod, code_root_resolved)
                )
            ]
            for name in to_remove:
                sys.modules.pop(name, None)

        strategy_class = getattr(module, strategy_class_name, None)
        if strategy_class is None:
            return StrategyCompileResult.failure(
                error_code="missing_required_class",
                errors=(f"module did not define class '{strategy_class_name}'",),
                error_dicts=(
                    {
                        "error_code": "missing_required_class",
                        "type": "missing_required_class",
                        "message": f"class '{strategy_class_name}' not found",
                    },
                ),
                repair_hints=(
                    f"Define a class named {strategy_class_name} inheriting Strategy.",
                ),
            )
        if not isinstance(strategy_class, type):
            return StrategyCompileResult.failure(
                error_code="not_a_class_definition",
                errors=(f"'{strategy_class_name}' is not a class definition",),
                error_dicts=(
                    {
                        "error_code": "not_a_class_definition",
                        "type": "not_a_class_definition",
                        "message": f"'{strategy_class_name}' is defined but is not a class",
                    },
                ),
            )
        if not issubclass(strategy_class, Strategy):
            return StrategyCompileResult.failure(
                error_code="invalid_base_class",
                errors=(f"class '{strategy_class_name}' must inherit from Strategy",),
                error_dicts=(
                    {
                        "error_code": "invalid_base_class",
                        "type": "invalid_base_class",
                        "message": f"class '{strategy_class_name}' does not subclass Strategy",
                        "expected": "Strategy",
                        "found": tuple(b.__name__ for b in strategy_class.__bases__),
                    },
                ),
                repair_hints=(
                    f"class {strategy_class_name}(Strategy): ...",
                ),
            )

        # Stage 2b: class-level config validation (timeframe / startup_history /
        # name). This runs the SAME guard as validate_definition so the
        # directory path — used by the authoring lifecycle and ``sdk validate``
        # — also rejects a ``timeframe`` the data layer cannot serve at compile
        # time instead of silently failing at runtime with ``data_insufficient``.
        attr_violation = _check_class_attributes(strategy_class)
        if attr_violation is not None:
            return StrategyCompileResult.failure(
                error_code=attr_violation.error_code,
                errors=(attr_violation.message,),
                error_dicts=(
                    {
                        "error_code": attr_violation.error_code,
                        "type": attr_violation.error_code,
                        "message": attr_violation.message,
                        **({"hint": attr_violation.hint} if attr_violation.hint else {}),
                        **attr_violation.extra,
                    },
                ),
                repair_hints=(attr_violation.hint,) if attr_violation.hint else (),
            )

        # Stage 3: Re-run the literal-history pass now that startup_history is
        # known from the executed class. Only emit history_check_literal_disallowed
        # violations — other checks already ran in stage 1.
        startup = getattr(strategy_class, "startup_history", None)
        if isinstance(startup, int) and not isinstance(startup, bool):
            history_violations: list[dict[str, Any]] = []
            for path in all_py:
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except SyntaxError:
                    # If it parsed in stage 1 it should parse here; skip defensively.
                    continue
                v = _StrategyASTVisitor(
                    strategy_startup_history=startup,
                    extra_allowed_modules=extra_allowed,
                )
                v.visit(tree)
                for viol in v.violations:
                    if viol.error_code == HISTORY_CHECK_LITERAL_DISALLOWED:
                        hd: dict[str, Any] = {
                            "error_code": viol.error_code,
                            "type": viol.error_code,
                            "message": viol.message,
                            "lineno": viol.lineno,
                            "col_offset": viol.col_offset,
                            "path": str(path.relative_to(code_root)),
                        }
                        if viol.hint:
                            hd["hint"] = viol.hint
                        hd.update(viol.extra)
                        history_violations.append(hd)
            if history_violations:
                return StrategyCompileResult.failure(
                    error_code=HISTORY_CHECK_LITERAL_DISALLOWED,
                    errors=tuple(v["message"] for v in history_violations),
                    error_dicts=tuple(history_violations),
                )

        descriptor = self._describe_strategy(strategy_class)
        artifact = CompiledStrategyArtifact(
            class_name=strategy_class_name,
            qualified_name=f"{module.__name__}.{strategy_class.__qualname__}",
            strategy_class=strategy_class,
            descriptor=descriptor,
        )
        return StrategyCompileResult.ok_result(artifact=artifact)

    def _parse_or_collect(
        self,
        path: "Path",
        source: str,
        sink: list[dict[str, Any]],
        *,
        code_root: "Path | None" = None,
    ) -> ast.AST | None:
        """Attempt to parse ``source``; on SyntaxError, append a violation dict to
        ``sink`` and return ``None``.

        When ``code_root`` is provided the ``path`` key in the violation dict is
        the subdirectory-preserving relative path (e.g. ``indicators/ma.py``),
        matching the format used by AST-violation dicts elsewhere in
        ``validate_directory``.  Without it the bare filename is used.
        """
        try:
            return ast.parse(source, filename=str(path), mode="exec")
        except SyntaxError as exc:
            p = Path(path)
            if code_root is not None:
                try:
                    display_path = p.relative_to(code_root).as_posix()
                except ValueError:
                    display_path = p.name
            else:
                display_path = p.name
            sink.append(
                {
                    "error_code": "syntax_error",
                    "type": "syntax_error",
                    "message": f"{display_path}: {exc.msg}",
                    "path": display_path,
                    "lineno": exc.lineno,
                }
            )
            return None

    # ----- Smoke test -----

    def smoke_test(
        self,
        artifact: CompiledStrategyArtifact,
        *,
        smoke_symbol: str = "__SMOKE__",
        max_history: int = 1000,
    ) -> StrategySmokeResult:
        """Run populate_indicators + on_bar against synthetic price regimes.

        Zero side effects. Catches:
        - ``__init__`` failure (e.g. Parameter validation rejecting defaults).
        - ``populate_indicators`` raising / returning non-DataFrame.
        - ``on_bar`` raising / returning non-Signal.
        - Most NaN-propagation bugs in indicator calculations.
        """
        import math
        from datetime import datetime, timedelta

        import pandas as pd

        try:
            strategy = artifact.strategy_class()
        except Exception as exc:  # noqa: BLE001
            return _smoke_failure(exc, "__init__", artifact.class_name)

        required = max(1, int(getattr(strategy, "startup_history", 30)))
        if required > max_history:
            required = max_history

        start = datetime(2026, 1, 1)

        def _df(closes: list[float], *, flat_range: bool = False) -> pd.DataFrame:
            highs = closes if flat_range else [c + 0.5 for c in closes]
            lows = closes if flat_range else [c - 0.5 for c in closes]
            return pd.DataFrame(
                {
                    "open": closes,
                    "high": highs,
                    "low": lows,
                    "close": closes,
                    "volume": [1_000_000.0] * len(closes),
                },
                index=pd.DatetimeIndex(
                    [start + timedelta(days=i) for i in range(len(closes))],
                    name="timestamp",
                ),
            )

        scenarios: list[tuple[str, pd.DataFrame]] = [
            ("monotone_up", _df([100.0 + 0.1 * i for i in range(required)])),
            ("flat", _df([100.0] * required, flat_range=True)),
            (
                "zigzag",
                _df(
                    [100.0 + 5.0 * math.sin(i * 2 * math.pi / 10) for i in range(required)]
                ),
            ),
        ]

        ctx = _make_smoke_context(smoke_symbol=smoke_symbol, as_of=start + timedelta(days=required))

        for scenario_name, df in scenarios:
            try:
                populated = strategy.populate_indicators(df, ctx)
            except Exception as exc:  # noqa: BLE001
                return _smoke_failure(
                    exc,
                    f"populate_indicators[{scenario_name}]",
                    artifact.class_name,
                )
            if not isinstance(populated, pd.DataFrame):
                return _smoke_shape_failure(
                    f"populate_indicators returned "
                    f"{type(populated).__name__} (expected DataFrame)",
                    artifact.class_name,
                    "populate_returned_non_df",
                )
            try:
                signal = strategy.on_bar(populated, ctx)
            except Exception as exc:  # noqa: BLE001
                return _smoke_failure(
                    exc,
                    f"on_bar[{scenario_name}]",
                    artifact.class_name,
                )
            if not isinstance(signal, Signal):
                return _smoke_shape_failure(
                    f"on_bar returned {type(signal).__name__} (expected Signal)",
                    artifact.class_name,
                    "on_bar_returned_non_signal",
                )

        return StrategySmokeResult(success=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_class_int_attr(
    tree: ast.AST, class_name: str, attr_name: str
) -> int | None:
    """Best-effort AST extraction of a literal int class attribute.

    Returns None if the attribute isn't a simple literal int (e.g. it's
    a computed expression). The visitor's history check only fires when
    we have a concrete int to compare against.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.target.id == attr_name and isinstance(stmt.value, ast.Constant):
                        if isinstance(stmt.value.value, int):
                            return stmt.value.value
                elif isinstance(stmt, ast.Assign):
                    if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        if stmt.targets[0].id == attr_name and isinstance(stmt.value, ast.Constant):
                            if isinstance(stmt.value.value, int):
                                return stmt.value.value
    return None


def _result_from_violations(
    code_hash: str, violations: list[_ASTViolation]
) -> StrategyCompileResult:
    """Build a CompileResult from a list of AST violations.

    Uses the first violation's error_code as the top-level summary, but
    surfaces ALL violations in ``validation_errors`` so the agent can fix
    them in one pass.
    """
    first = violations[0]
    return StrategyCompileResult(
        success=False,
        code_hash=code_hash,
        error_code=first.error_code,
        errors=tuple(
            f"line {v.lineno}: {v.message} [{v.error_code}]" for v in violations
        ),
        validation_errors=tuple(
            {
                "type": v.error_code,
                "message": v.message,
                "lineno": v.lineno,
                "col_offset": v.col_offset,
                **v.extra,
            }
            for v in violations
        ),
        repair_hints=tuple(v.hint for v in violations if v.hint),
    )


def _check_class_attributes(strategy_class: type) -> _ASTViolation | None:
    """Validate types and ranges of Strategy class-level configuration."""

    timeframe = getattr(strategy_class, "timeframe", None)
    if not isinstance(timeframe, str) or timeframe not in _VALID_TIMEFRAMES:
        return _ASTViolation(
            error_code=INVALID_CLASS_ATTRIBUTE,
            message=(
                f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}, "
                f"got {timeframe!r}"
            ),
            lineno=0,
            col_offset=0,
            hint='timeframe = "1d" / "1w" / "60m" / "5m" etc.',
            extra={"attribute": "timeframe", "value": repr(timeframe)},
        )
    startup_history = getattr(strategy_class, "startup_history", None)
    if (
        not isinstance(startup_history, int)
        or isinstance(startup_history, bool)
        or startup_history < 1
    ):
        return _ASTViolation(
            error_code=INVALID_CLASS_ATTRIBUTE,
            message=(
                f"startup_history must be a positive int, got {startup_history!r}"
            ),
            lineno=0,
            col_offset=0,
            hint="startup_history = 30  # accommodate your longest rolling window",
            extra={"attribute": "startup_history", "value": repr(startup_history)},
        )
    name = getattr(strategy_class, "name", "")
    if not isinstance(name, str):
        return _ASTViolation(
            error_code=INVALID_CLASS_ATTRIBUTE,
            message=f"name must be a string, got {type(name).__name__}",
            lineno=0,
            col_offset=0,
            extra={"attribute": "name", "value": repr(name)},
        )
    return None


def _smoke_failure(
    exc: BaseException, stage: str, class_name: str
) -> StrategySmokeResult:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    excerpt = tb.splitlines()[-1] if tb else f"{type(exc).__name__}: {exc}"
    # If the exception is a StrategyError, propagate its error_code.
    error_code = "runtime_smoke_failed"
    hint = (
        f"{stage} raised during smoke. Fix the underlying error before "
        "the strategy hits a real cycle."
    )
    extra: dict[str, Any] = {}
    sdk_code = getattr(exc, "error_code", None)
    if isinstance(sdk_code, str):
        extra["sdk_error_code"] = sdk_code
        hint = getattr(exc, "hint", "") or hint
    return StrategySmokeResult(
        success=False,
        error_code=error_code,
        error_type=type(exc).__name__,
        error_message=f"{type(exc).__name__}: {exc}",
        traceback_excerpt=excerpt,
        validation_errors=(
            {
                "type": error_code,
                "stage": stage,
                "class_name": class_name,
                "error_type": type(exc).__name__,
                "message": str(exc),
                **extra,
            },
        ),
        repair_hints=(hint,),
    )


def _smoke_shape_failure(
    message: str, class_name: str, sub_type: str
) -> StrategySmokeResult:
    return StrategySmokeResult(
        success=False,
        error_code="smoke_output_invalid",
        error_type="ValueError",
        error_message=message,
        traceback_excerpt=message,
        validation_errors=(
            {
                "type": "smoke_output_invalid",
                "subtype": sub_type,
                "class_name": class_name,
                "message": message,
            },
        ),
        repair_hints=(
            "populate_indicators must return DataFrame; on_bar must return "
            "Signal.buy(tag=...) / Signal.sell(tag=...) / Signal.hold().",
        ),
    )


def _make_smoke_context(*, smoke_symbol: str, as_of: Any) -> Any:
    """Build a minimal :class:`StrategyContext` for smoke tests.

    Uses a no-op DataProvider stub — strategies that try to call
    ``ctx.dp.*`` during smoke will fail with a clear AttributeError that
    surfaces via the smoke result. (Real-data smoke is impractical; the
    smoke test is to catch shape / NaN / typo bugs, not data integration.)
    """
    from decimal import Decimal as _Dec

    from doyoutrade.strategy_sdk.context import (
        AccountView,
        PositionView,
        StrategyContext,
    )

    class _SmokeDataProvider:
        is_backtest = True

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)

            def _raise(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError(
                    f"ctx.dp.{name}() called during smoke test; smoke uses "
                    "synthetic data only. Wrap cross-symbol logic so it's "
                    "skipped when data is unavailable, or ensure your test "
                    "doesn't depend on real data fetches."
                )

            return _raise

    return StrategyContext(
        symbol=smoke_symbol,
        now=as_of,
        run_id="",
        trace_id="",
        universe=(smoke_symbol,),
        position=PositionView(symbol=smoke_symbol, quantity=0.0, cost_price=_Dec("0")),
        account=AccountView(cash=_Dec("100000"), equity=_Dec("100000")),
        params={},
        dp=_SmokeDataProvider(),  # type: ignore[arg-type]
    )


__all__ = [
    "CompiledStrategyArtifact",
    "StrategyCompileResult",
    "StrategyCompiler",
    "StrategySmokeResult",
]
