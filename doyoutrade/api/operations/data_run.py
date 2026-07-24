"""Unified ``data_run`` operation — OHLCV + indicators + scripted factors.

Compared to the original single-symbol implementation, this rewrite adds:

* **Multi-symbol fan-out** via ``code`` (single), ``symbols`` (CSV list), or
  ``universe_file`` (one canonical symbol per line). Mutually exclusive.
  Per-symbol failures are reported as ``symbols[i].status == "failed"`` with
  a stable ``error_code`` — they never collapse the whole run.
* **AST sandbox for custom scripts** reusing
  ``doyoutrade.strategy_runtime.compiler._ALLOWED_IMPORTS`` and the same
  "silent fallback" checks the strategy compiler applies (lookahead access,
  silent broad except, silent type coercion). Imports outside the whitelist
  raise ``script_disallowed_import`` at compile time; the same rule fires
  via :func:`_safe_import` if any reachable code path tries a dynamic
  ``__import__``.
* **Per-script ``REQUIRED_HISTORY`` literal**, AST-extracted, fed into the
  warmup resolver. A pure-script run with no warmup hint anywhere raises
  ``script_warmup_unspecified`` so the caller picks explicitly instead of
  silently getting an empty lookback frame.
* **Compute signature check** via :mod:`inspect` — ``compute`` must accept
  exactly ``(df, target_df, params)``; other shapes raise
  ``script_compute_signature_invalid``.
* **Thread-pool execution with timeout** — scripts run in a worker thread
  via :func:`asyncio.to_thread` wrapped in :func:`asyncio.wait_for`. A
  runaway loop yields ``script_timeout`` and the original request returns
  promptly; the orphan thread is acknowledged but not killed (Python can't).
* **Sub-typed runtime failures** — ``NameError`` / ``KeyError`` /
  ``AttributeError`` / ``ImportError`` / ``TypeError`` each map to their
  own ``error_code`` so the agent can see distinct failure modes.
* **No silent scalar broadcast** — dict outputs may only carry
  :class:`pandas.Series`, :class:`list`, or :class:`pandas.DataFrame`
  columns aligned to the requested window. Scalars raise
  ``script_output_scalar_broadcast`` (legacy behaviour was to broadcast
  silently, masking the common "forgot to return a Series" bug).
* **Script source not echoed in envelope** — replaced with
  ``script_source = {kind, path, sha256, bytes, required_history}``; the
  raw source is written to ``script_<sha>.py`` next to the OHLCV / indicator
  artifacts so debug exports stay small.

Top-level / per-symbol debug events:

* ``operation_data_run.request`` — input keys + symbol count
* ``operation_data_run.rejected`` — unknown_arguments
* ``operation_data_run.failed`` — global validation failure (no per-symbol work)
* ``operation_data_run.symbol.started`` / ``.validated`` / ``.completed`` /
  ``.failed`` — per-symbol lifecycle, payload includes ``code`` and the
  relevant ``error_code`` on failure
* ``operation_data_run.created`` — final envelope summary
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import inspect
import json
import logging
import math
import re
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd

from doyoutrade.api.operations.indicators_compute import (
    IndicatorComputeTool,
    _InvalidArgument as _IndicatorInvalidArgument,
)
from doyoutrade.api.operations.market_data import (
    MarketDataFetcher,
    _ConflictingRange,
    _IntervalNotSupportedForSymbol,
    _InvalidDate,
    _InvalidPeriod,
    _get_artifacts_root,
    _safe_code,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.strategy_runtime.compiler import _ALLOWED_IMPORTS as _STRATEGY_ALLOWED_IMPORTS
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
_SUPPORTED_DATA_SOURCES = ("auto", "qmt", "akshare", "tushare", "baostock", "mootdx")

# Whitelisted imports for ``data run`` scripts. Single-sourced with
# StrategyCompiler so a strategy-author's mental model carries over.
_DATA_SCRIPT_ALLOWED_IMPORTS: frozenset[str] = _STRATEGY_ALLOWED_IMPORTS

# Restricted builtins available to scripts. Mirrors the strategy sandbox
# but a few quality-of-life entries (``print``) are intentionally omitted —
# scripts return values, they don't side-effect.
_DATA_SCRIPT_BUILTINS = MappingProxyType(
    {
        name: getattr(builtins, name)
        for name in (
            "abs", "all", "any", "bool", "dict", "enumerate", "filter",
            "float", "frozenset", "getattr", "hasattr", "int", "isinstance",
            "issubclass", "len", "list", "map", "max", "min", "next", "pow",
            "range", "repr", "reversed", "round", "set", "slice", "sorted",
            "str", "sum", "tuple", "zip",
        )
    }
)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class _InvalidDataRunArgument(ValueError):
    """Structured argument failure carrying a stable ``error_code``.

    ``error_type`` is populated when the failure originates from a
    sub-typed exception (script ``NameError`` etc.) so the debug event
    surface stays distinguishable.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        hint: str | None = None,
        *,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.error_type = error_type


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _tail_values(series: pd.Series, tail: int) -> list[Any]:
    return [_jsonable(v) for v in series.tail(tail).tolist()]


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    """Read a positive integer indicator param.

    Raises ``_InvalidDataRunArgument`` rather than coercing — a fractional
    or non-numeric value is a real bug in the caller's payload, not an
    edge case we should round away. Mirrors the rule in §错误可见性.
    """

    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidDataRunArgument(
            "invalid_indicator_param",
            f"param {key!r} must be an integer, got {type(value).__name__}({value!r})",
        )
    intval = int(value)
    if float(value) != intval or intval < 1:
        raise _InvalidDataRunArgument(
            "invalid_indicator_param",
            f"param {key!r} must be a positive integer, got {value!r}",
        )
    return intval


# ---------------------------------------------------------------------------
# Built-in indicator warmup estimator
#
# Conservative bar-counts per built-in. Kept in sync with the dispatch
# table in ``indicators_compute.py``; new indicators land here too.
# ---------------------------------------------------------------------------


def _estimate_indicator_warmup(name: str, params: dict[str, Any]) -> int:
    if name == "sma":
        return _int_param(params, "window", 20)
    if name == "ema":
        return _int_param(params, "span", 20) * 4
    if name == "rsi":
        return _int_param(params, "period", 14) * 4
    if name == "macd":
        slow = _int_param(params, "slow", 26)
        signal = _int_param(params, "signal", 9)
        return slow + signal + slow * 3
    if name == "bollinger":
        return _int_param(params, "window", 20)
    if name in {"atr", "adx", "mfi", "supertrend"}:
        return _int_param(params, "period", 14 if name != "supertrend" else 10) * 4
    if name == "obv":
        return 2
    if name == "kdj":
        return _int_param(params, "n", 9) + (
            _int_param(params, "k_smooth", 3) + _int_param(params, "d_smooth", 3)
        ) * 4
    if name in {"williams_r", "cci", "cmf"}:
        return _int_param(params, "period", 14 if name == "williams_r" else 20)
    if name in {"roc", "momentum"}:
        return _int_param(params, "period", 12 if name == "roc" else 10) + 1
    if name == "trix":
        return _int_param(params, "period", 15) * 3 * 4 + 1
    if name == "vwap":
        return _int_param(params, "window", 14)
    if name == "ad":
        return 2
    if name == "volume_ratio":
        return _int_param(params, "window", 20)
    if name == "keltner":
        return max(
            _int_param(params, "ema_window", 20) * 4,
            _int_param(params, "atr_period", 10) * 4,
        )
    if name in {"donchian", "stdev", "wma"}:
        return _int_param(params, "window", 20)
    if name == "hist_volatility":
        return _int_param(params, "window", 20) + 1
    if name in {"dema", "kama"}:
        return _int_param(
            params, "span" if name == "dema" else "period", 20 if name == "dema" else 10
        ) * 4
    if name == "psar":
        return 5
    if name == "ichimoku":
        return _int_param(params, "senkou_b", 52) + _int_param(params, "kijun", 26)
    if name in {"limit_up_approx", "limit_down_approx"}:
        return 2
    return 0


# ---------------------------------------------------------------------------
# Script AST validator
#
# Mirrors the relevant subset of _StrategyASTVisitor:
#
# - import whitelist (script_disallowed_import)
# - df.iloc[i>=0] / df.shift(-N) lookahead (script_lookahead_access)
# - silent broad except (script_silent_exception_swallow)
# - silent isinstance fallback (script_silent_type_coercion)
#
# Strategy-specific checks (Signal.tag, ctx.dp, populate_indicators, etc.)
# don't apply to data scripts and are intentionally skipped.
# ---------------------------------------------------------------------------


class _ScriptViolation:
    __slots__ = ("error_code", "message", "lineno", "hint")

    def __init__(self, error_code: str, message: str, lineno: int, hint: str = "") -> None:
        self.error_code = error_code
        self.message = message
        self.lineno = lineno
        self.hint = hint


class _DataScriptValidator(ast.NodeVisitor):
    """AST pass enforcing the data-script sandbox contract."""

    def __init__(self) -> None:
        self.violations: list[_ScriptViolation] = []

    # ----- imports -----

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not self._is_allowed_import(alias.name):
                self.violations.append(
                    _ScriptViolation(
                        "script_disallowed_import",
                        f"disallowed import: {alias.name}",
                        node.lineno,
                        hint=(
                            "Scripts may only import "
                            f"{sorted(_DATA_SCRIPT_ALLOWED_IMPORTS)}. "
                            "Data access goes through df / target_df / indicators."
                        ),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if not self._is_allowed_import(module):
            self.violations.append(
                _ScriptViolation(
                    "script_disallowed_import",
                    f"disallowed import: from {module}",
                    node.lineno,
                    hint=(
                        "Scripts may only import "
                        f"{sorted(_DATA_SCRIPT_ALLOWED_IMPORTS)}."
                    ),
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_allowed_import(module: str) -> bool:
        if not module:
            return False
        for allowed in _DATA_SCRIPT_ALLOWED_IMPORTS:
            if module == allowed or module.startswith(allowed + "."):
                return True
        return False

    # ----- silent exception swallow -----

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if self._is_broad_except(node) and self._is_silent_body(node.body):
            self.violations.append(
                _ScriptViolation(
                    "script_silent_exception_swallow",
                    f"silent broad except at line {node.lineno} hides failures",
                    node.lineno,
                    hint=(
                        "Narrow the exception type and add logging, or let "
                        "the failure propagate so data_run reports it."
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
        if len(body) != 1:
            return False
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if isinstance(stmt, ast.Continue):
            return True
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return True
        return False

    # ----- silent type coercion -----

    def visit_If(self, node: ast.If) -> None:
        if self._is_silent_isinstance_fallback(node):
            self.violations.append(
                _ScriptViolation(
                    "script_silent_type_coercion",
                    "silent type coercion 'if not isinstance(x, T): x = default' "
                    "hides schema violations",
                    node.lineno,
                    hint=(
                        "Raise ValueError / TypeError with the actual type "
                        "instead of overwriting the variable."
                    ),
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_silent_isinstance_fallback(node: ast.If) -> bool:
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

    # ----- lookahead access -----
    #
    # ``df.iloc[N>=0]`` is *not* lookahead in a data-script context because
    # the script runs on a fixed window — positional indexing into "the
    # first / N-th bar of the window" is a legitimate operation (e.g.
    # "gap from the start of the fetch window"). That check is therefore
    # omitted here; the strategy compiler keeps it for per-bar code where
    # the cursor / current bar concept makes ``iloc[0]`` ambiguous.
    #
    # ``df.shift(-N)`` *is* lookahead in any context — it shifts data N
    # bars from the future into the current row — so it stays banned.

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_lookahead_shift(node):
            self.violations.append(
                _ScriptViolation(
                    "script_lookahead_access",
                    "lookahead access: df.shift(-N) shifts data from the future",
                    node.lineno,
                    hint="Use df.shift(N) with N>=1.",
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_lookahead_shift(node: ast.Call) -> bool:
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "shift"):
            return False
        if not node.args:
            return False
        arg = node.args[0]
        if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
            return True
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
            return arg.value < 0
        return False


def _extract_script_required_history(tree: ast.AST) -> int | None:
    """AST-extract a top-level ``REQUIRED_HISTORY = <int literal>`` assignment.

    Only accepts a plain literal (no expressions, no class attributes).
    Returns ``None`` when the script doesn't declare one — the caller
    decides whether absence is an error (pure-script no-warmup case).
    """

    if not isinstance(tree, ast.Module):
        return None
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if (
                isinstance(target, ast.Name)
                and target.id == "REQUIRED_HISTORY"
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, int)
                and not isinstance(stmt.value.value, bool)
            ):
                return stmt.value.value
        elif isinstance(stmt, ast.Assign):
            if (
                len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "REQUIRED_HISTORY"
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, int)
                and not isinstance(stmt.value.value, bool)
            ):
                return stmt.value.value
    return None


# ---------------------------------------------------------------------------
# Sandboxed import for script runtime
# ---------------------------------------------------------------------------


def _safe_script_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    if level != 0:
        raise ImportError("relative imports are not allowed")
    root = name.split(".", 1)[0]
    if name not in _DATA_SCRIPT_ALLOWED_IMPORTS and root not in _DATA_SCRIPT_ALLOWED_IMPORTS:
        raise ImportError(f"disallowed import: {name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


# ---------------------------------------------------------------------------
# Symbol input normalization
# ---------------------------------------------------------------------------


_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# First-column header tokens we skip when a universe file is a CSV with a
# header row (``symbol,name`` etc.). Lowercased; checked against the first
# CSV column of the first non-comment line only.
_UNIVERSE_HEADER_TOKENS = frozenset(
    {
        "symbol",
        "symbols",
        "code",
        "codes",
        "ticker",
        "stock",
        "stock_code",
        "sec_code",
        "secid",
        "instrument",
    }
)


def _parse_csv_symbols(value: Any) -> list[str]:
    """Accept ``["A.SH", "B.SZ"]`` / ``"A.SH,B.SZ"`` / JSON array string."""

    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _InvalidDataRunArgument(
                    "invalid_symbols",
                    f"symbols looked like JSON but failed to parse: {exc}",
                )
            if not isinstance(parsed, list):
                raise _InvalidDataRunArgument(
                    "invalid_symbols",
                    f"symbols JSON must be an array, got {type(parsed).__name__}",
                )
            items = parsed
        else:
            items = [item.strip() for item in text.split(",") if item.strip()]
    else:
        raise _InvalidDataRunArgument(
            "invalid_symbols",
            f"symbols must be a list or comma string, got {type(value).__name__}",
        )
    return [str(item).strip() for item in items if str(item).strip()]


def _load_universe_file(path_value: Any) -> list[str]:
    if not isinstance(path_value, str) or not path_value.strip():
        raise _InvalidDataRunArgument(
            "invalid_universe_file",
            "universe_file must be a non-empty path",
        )
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        raise _InvalidDataRunArgument(
            "universe_file_not_found",
            f"universe file not found: {path}",
            "pass an existing readable file with one CODE.EXCHANGE per line",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise _InvalidDataRunArgument(
            "universe_file_read_failed",
            f"failed to read universe file {path}: {exc}",
        ) from exc
    out: list[str] = []
    csv_columns_taken = 0
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        # Accept CSV exports (pandas ``to_csv`` is the agent's reflex): take
        # the first column as the symbol. A bad first field still fails
        # ``_validate_canonical_codes`` below, so this never masks errors.
        candidate = stripped
        if "," in candidate:
            candidate = candidate.split(",", 1)[0].strip()
            csv_columns_taken += 1
        if not candidate:
            continue
        # Skip a single leading header row (``symbol,name`` / ``code`` / …)
        # so a CSV with headers does not surface as ``invalid_symbol`` on the
        # header literal (tmp/messages.json turn 10).
        if not out and candidate.lower() in _UNIVERSE_HEADER_TOKENS:
            logger.info("universe_file header row skipped: %r", stripped)
            continue
        out.append(candidate)
    if csv_columns_taken:
        logger.info(
            "universe_file: took first CSV column for %d line(s)", csv_columns_taken
        )
    return out


def _validate_canonical_codes(codes: list[str]) -> list[str]:
    """De-dup preserving order; reject empties / obviously-wrong shapes.

    Symbols must be canonical (``CODE.EXCHANGE``). We don't query the
    instrument catalog here — that's the caller's job (``stock lookup``).
    But we still gate on a basic charset so a stray comma or non-string
    surfaces as ``invalid_symbol`` instead of leaking into the provider
    call.
    """

    seen: dict[str, None] = {}
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            raise _InvalidDataRunArgument(
                "invalid_symbol",
                f"symbol must be a non-empty string, got {code!r}",
            )
        normalized = code.strip()
        if not _SYMBOL_PATTERN.match(normalized):
            raise _InvalidDataRunArgument(
                "invalid_symbol",
                f"symbol {normalized!r} contains unsupported characters; "
                "use canonical CODE.EXCHANGE form",
                "resolve via doyoutrade-cli stock lookup first",
            )
        seen.setdefault(normalized, None)
    if not seen:
        raise _InvalidDataRunArgument(
            "no_symbols",
            "no symbols resolved from input",
            "pass at least one canonical CODE.EXCHANGE",
        )
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Script source + executor
# ---------------------------------------------------------------------------


class _ScriptSource:
    """Validated, compiled, hashed script source."""

    __slots__ = ("kind", "source_path", "code", "sha256", "compiled", "required_history")

    def __init__(
        self,
        *,
        kind: str,
        source_path: str | None,
        code: str,
        sha256: str,
        compiled: Any,
        required_history: int | None,
    ) -> None:
        self.kind = kind
        self.source_path = source_path
        self.code = code
        self.sha256 = sha256
        self.compiled = compiled
        self.required_history = required_history

    def metadata(self, *, persisted_path: str | None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "kind": self.kind,
            "sha256": self.sha256,
            "bytes": len(self.code.encode("utf-8")),
            "required_history": self.required_history,
        }
        if self.kind == "file" and self.source_path:
            meta["source_path"] = self.source_path
        if persisted_path:
            meta["persisted_path"] = persisted_path
        return meta


def _prepare_script_source(
    *,
    script: Any,
    script_file: Any,
) -> _ScriptSource | None:
    """Validate + AST-check + compile a script. Returns ``None`` when neither
    ``script`` nor ``script_file`` is provided."""

    if script is not None and script_file is not None:
        raise _InvalidDataRunArgument(
            "conflicting_script_args",
            "script and script_file cannot be combined",
            "pass either --script or --script-file",
        )
    if script is None and script_file is None:
        return None

    if script is not None:
        if not isinstance(script, str) or not script.strip():
            raise _InvalidDataRunArgument("script_invalid", "script must be a non-empty string")
        source = script
        kind = "inline"
        source_path: str | None = None
    else:
        if not isinstance(script_file, str) or not script_file.strip():
            raise _InvalidDataRunArgument(
                "script_file_path_invalid",
                "script_file must be a non-empty path",
            )
        path = Path(script_file).expanduser()
        if not path.exists() or not path.is_file():
            raise _InvalidDataRunArgument(
                "script_file_not_found",
                f"script file not found: {path}",
                "pass an existing readable .py file",
            )
        try:
            source = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("data_run failed to read script_file=%s err=%s", path, exc)
            raise _InvalidDataRunArgument(
                "script_file_read_failed",
                f"failed to read script file {path}: {exc}",
            ) from exc
        kind = "file"
        source_path = str(path)

    # Parse + AST validate.
    filename = source_path or "<data-run-script>"
    try:
        tree = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        raise _InvalidDataRunArgument(
            "script_syntax_error",
            f"script syntax error at line {exc.lineno}: {exc.msg}",
            "fix the SyntaxError and retry",
        ) from exc

    validator = _DataScriptValidator()
    validator.visit(tree)
    if validator.violations:
        first = validator.violations[0]
        # Surface every violation in the message body so the model sees
        # them all even though the top-level error_code is the first one.
        details = "; ".join(
            f"line {v.lineno}: {v.message} [{v.error_code}]"
            for v in validator.violations
        )
        raise _InvalidDataRunArgument(
            first.error_code,
            f"{first.message} (line {first.lineno}). All violations: {details}",
            first.hint or None,
        )

    required_history = _extract_script_required_history(tree)
    if required_history is not None and required_history < 0:
        raise _InvalidDataRunArgument(
            "script_required_history_invalid",
            f"REQUIRED_HISTORY must be >= 0, got {required_history}",
        )

    try:
        compiled = compile(tree, filename, "exec")
    except Exception as exc:  # noqa: BLE001 -- compile() of an already-parsed tree should not fail; surface if it does
        raise _InvalidDataRunArgument(
            "script_compile_failed",
            f"script failed to compile after AST check: {exc}",
        ) from exc

    sha = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return _ScriptSource(
        kind=kind,
        source_path=source_path,
        code=source,
        sha256=sha,
        compiled=compiled,
        required_history=required_history,
    )


def _exec_script_sync(
    *,
    source: _ScriptSource,
    fetch_df: pd.DataFrame,
    target_df: pd.DataFrame,
    params: dict[str, Any],
) -> Any:
    """Run a validated script in a restricted namespace.

    Called from a worker thread via :func:`asyncio.to_thread`. Returns the
    raw object the script produced (``compute(...)`` return value, or the
    ``result`` global). The orchestrator coerces it into per-column Series.

    Raises distinct :class:`_InvalidDataRunArgument` subclasses for each
    runtime failure mode (NameError → script_name_error, etc.) so the
    caller's debug event payload stays informative.
    """

    from doyoutrade.strategy_sdk import indicators

    import numpy as np  # local — keep the global module surface small

    restricted = dict(_DATA_SCRIPT_BUILTINS)
    restricted["__import__"] = _safe_script_import

    namespace: dict[str, Any] = {
        "__builtins__": restricted,
        "__name__": "doyoutrade.data_run.script",
        "pd": pd,
        "np": np,
        "indicators": indicators,
        "df": fetch_df.copy(),
        "target_df": target_df.copy(),
        "params": dict(params),
    }

    try:
        exec(source.compiled, namespace, namespace)
    except _InvalidDataRunArgument:
        raise
    except NameError as exc:
        raise _InvalidDataRunArgument(
            "script_name_error",
            f"script NameError: {exc}",
            "check spelling of df/target_df/params/indicators/pd/np and any helpers",
            error_type="NameError",
        ) from exc
    except ImportError as exc:
        raise _InvalidDataRunArgument(
            "script_import_error",
            f"script ImportError: {exc}",
            f"only these imports are allowed: {sorted(_DATA_SCRIPT_ALLOWED_IMPORTS)}",
            error_type="ImportError",
        ) from exc
    except KeyError as exc:
        raise _InvalidDataRunArgument(
            "script_key_error",
            f"script KeyError: {exc!r}",
            "params / df column access used a key that doesn't exist",
            error_type="KeyError",
        ) from exc
    except AttributeError as exc:
        raise _InvalidDataRunArgument(
            "script_attribute_error",
            f"script AttributeError: {exc}",
            "verify the attribute exists on the object",
            error_type="AttributeError",
        ) from exc
    except TypeError as exc:
        raise _InvalidDataRunArgument(
            "script_type_error",
            f"script TypeError: {exc}",
            "check call signatures and value types",
            error_type="TypeError",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — generic fallback with error_type attached
        raise _InvalidDataRunArgument(
            "script_runtime_error",
            f"script raised {type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
        ) from exc

    compute = namespace.get("compute")
    if callable(compute):
        _validate_compute_signature(compute)
        try:
            return compute(namespace["df"], namespace["target_df"], namespace["params"])
        except _InvalidDataRunArgument:
            raise
        except NameError as exc:
            raise _InvalidDataRunArgument(
                "script_name_error",
                f"compute() NameError: {exc}",
                error_type="NameError",
            ) from exc
        except KeyError as exc:
            raise _InvalidDataRunArgument(
                "script_key_error",
                f"compute() KeyError: {exc!r}",
                error_type="KeyError",
            ) from exc
        except AttributeError as exc:
            raise _InvalidDataRunArgument(
                "script_attribute_error",
                f"compute() AttributeError: {exc}",
                error_type="AttributeError",
            ) from exc
        except ImportError as exc:
            raise _InvalidDataRunArgument(
                "script_import_error",
                f"compute() ImportError: {exc}",
                error_type="ImportError",
            ) from exc
        except TypeError as exc:
            raise _InvalidDataRunArgument(
                "script_type_error",
                f"compute() TypeError: {exc}",
                error_type="TypeError",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise _InvalidDataRunArgument(
                "script_runtime_error",
                f"compute() raised {type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            ) from exc

    if "result" in namespace:
        return namespace["result"]
    raise _InvalidDataRunArgument(
        "script_no_result",
        "custom script must define compute(df, target_df, params) or assign a 'result' global",
        "either return from compute(...) or set result = {...}",
    )


def _validate_compute_signature(compute: Any) -> None:
    """Reject ``compute`` shapes other than ``(df, target_df, params)``.

    Accept positional-or-keyword params with those exact names, in that
    exact order. *args / **kwargs are rejected — too permissive surfaces
    spelling bugs as silent broadcasts.
    """

    try:
        sig = inspect.signature(compute)
    except (TypeError, ValueError) as exc:
        raise _InvalidDataRunArgument(
            "script_compute_signature_invalid",
            f"compute() signature could not be inspected: {exc}",
        ) from exc

    params = list(sig.parameters.values())
    expected = ("df", "target_df", "params")
    if len(params) != 3:
        raise _InvalidDataRunArgument(
            "script_compute_signature_invalid",
            f"compute() must take exactly 3 params (df, target_df, params); got {len(params)}",
        )
    for actual, want in zip(params, expected):
        if actual.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            raise _InvalidDataRunArgument(
                "script_compute_signature_invalid",
                f"compute() param {actual.name!r} must be positional-or-keyword, got {actual.kind.name}",
            )
        if actual.name != want:
            raise _InvalidDataRunArgument(
                "script_compute_signature_invalid",
                f"compute() params must be named (df, target_df, params); got {tuple(p.name for p in params)}",
            )


def _coerce_script_result(raw: Any, target_index: pd.Index) -> dict[str, pd.Series]:
    """Coerce a script's return value into per-column Series.

    Allowed shapes:
    - ``pd.Series`` → single column (uses ``.name`` or 'value')
    - ``pd.DataFrame`` → one column per dataframe column
    - ``dict[str, Series | list]`` → one column per key

    Lists must match ``target_index`` length. Scalars in dicts were
    previously broadcast silently; that's now ``script_output_scalar_broadcast``
    so the common "forgot to return a Series" bug is visible.
    """

    if isinstance(raw, pd.Series):
        return {str(raw.name) if raw.name is not None else "value": raw}
    if isinstance(raw, pd.DataFrame):
        out_df: dict[str, pd.Series] = {}
        for col in raw.columns:
            column_data = raw[col]
            if isinstance(column_data, pd.DataFrame):
                raise _InvalidDataRunArgument(
                    "script_output_invalid",
                    f"DataFrame column {col!r} resolves to multiple sub-columns; "
                    "rename duplicate columns or return a flat dict instead",
                )
            out_df[str(col)] = column_data
        return out_df
    if isinstance(raw, dict):
        out: dict[str, pd.Series] = {}
        for key, value in raw.items():
            name = str(key).strip()
            if not name:
                raise _InvalidDataRunArgument(
                    "script_output_invalid",
                    "custom output contains an empty key",
                )
            if isinstance(value, pd.Series):
                out[name] = value
            elif isinstance(value, pd.DataFrame):
                raise _InvalidDataRunArgument(
                    "script_output_invalid",
                    f"custom output {name!r} is a DataFrame; "
                    "return DataFrame as the top-level value or split into columns yourself",
                )
            elif isinstance(value, list):
                if len(value) != len(target_index):
                    raise _InvalidDataRunArgument(
                        "script_output_invalid",
                        f"custom output {name!r} list length {len(value)} != target rows {len(target_index)}",
                    )
                out[name] = pd.Series(value, index=target_index)
            elif isinstance(value, (int, float, str)) or value is None:
                raise _InvalidDataRunArgument(
                    "script_output_scalar_broadcast",
                    f"custom output {name!r} is a scalar ({type(value).__name__}={value!r}); "
                    "return a pandas Series / list aligned to target_df.index instead",
                    "scalar broadcasting masks the 'forgot to return a Series' bug",
                )
            else:
                raise _InvalidDataRunArgument(
                    "script_output_invalid",
                    f"custom output {name!r} has unsupported type {type(value).__name__}",
                )
        if not out:
            raise _InvalidDataRunArgument(
                "script_output_invalid",
                "custom script returned an empty dict",
            )
        return out
    raise _InvalidDataRunArgument(
        "script_output_invalid",
        f"custom script must return Series, DataFrame, or non-empty dict, got {type(raw).__name__}",
    )


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------


class DataRunTool(OperationHandler):
    name = "data_run"
    description = (
        "Fetch OHLCV for one or many symbols and optionally compute built-in "
        "SDK indicators plus AST-sandboxed local Python indicator scripts. "
        "Symbols are taken from ``code`` (single), ``symbols`` (CSV/JSON "
        "list), or ``universe_file`` — exactly one. Each symbol's OHLCV is "
        "trimmed to the requested window; the provider fetch can start "
        "earlier when warm-up is needed (auto-sized from selected built-ins "
        "and a script's REQUIRED_HISTORY literal, or set explicitly via "
        "warmup_bars). Per-symbol failures surface as symbols[i].status == "
        "'failed' with a stable error_code; they never collapse the run."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Single canonical CODE.EXCHANGE symbol."},
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of canonical symbols. CSV string also accepted at CLI layer.",
            },
            "universe_file": {
                "type": "string",
                "description": "Path to a file with one CODE.EXCHANGE per line (# comments ok).",
            },
            "period": {"type": "string"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "interval": {"type": "string", "default": "1d"},
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_DATA_SOURCES),
                "default": "auto",
            },
            "indicators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Built-in indicator names, comma string, JSON array string, or 'all'.",
            },
            "indicator_params": {
                "type": "object",
                "description": "Per-indicator parameter overrides.",
            },
            "script": {"type": "string", "description": "Inline Python factor script."},
            "script_file": {"type": "string", "description": "Path to Python factor script."},
            "script_params": {"type": "object", "description": "Params passed to compute()."},
            "script_timeout": {
                "type": "number",
                "minimum": 0.1,
                "description": "Per-symbol script execution timeout in seconds (default 10).",
            },
            "warmup_bars": {
                "type": "integer",
                "description": "Explicit warm-up bars. Omit for auto-sizing.",
                "minimum": 0,
            },
            "tail": {"type": "integer", "default": 1, "minimum": 1},
        },
        "additionalProperties": False,
    }

    coercion_rules = (
        SchemaCoercion(
            field="symbols",
            declared_type="array",
            item_type=str,
            error_code="invalid_symbols",
        ),
        SchemaCoercion(
            field="indicators",
            declared_type="array",
            item_type=str,
            error_code="invalid_indicators_json",
        ),
        SchemaCoercion(
            field="indicator_params",
            declared_type="object",
            error_code="invalid_indicator_params_json",
        ),
        SchemaCoercion(
            field="script_params",
            declared_type="object",
            error_code="invalid_script_params_json",
        ),
    )

    _DEFAULT_SCRIPT_TIMEOUT_SECONDS = 10.0

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_run.rejected",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            return ToolResult(
                text=format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                ),
                is_error=True,
            )
        kwargs = dict(contract.kwargs)

        # The CLI sends ``indicators`` / ``symbols`` as bare strings the
        # array coercion would reject; pull them aside before the generic
        # coercion pass and re-attach as lists afterwards.
        indicators_raw = kwargs.get("indicators")
        indicators_is_string = isinstance(indicators_raw, str)
        all_indicators_requested = (
            indicators_is_string and indicators_raw.strip().lower() == "all"
        )
        if indicators_is_string:
            kwargs.pop("indicators", None)
        symbols_raw = kwargs.get("symbols")
        symbols_is_string = isinstance(symbols_raw, str)
        if symbols_is_string:
            kwargs.pop("symbols", None)

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            err = coercion.error
            await emit_debug_event("operation_data_run.failed", {"tool": self.name, **err})
            return ToolResult(
                text=format_error_text(
                    str(err.get("error_code") or "validation_error"),
                    str(err.get("error") or "invalid input"),
                    err.get("hint") if isinstance(err.get("hint"), str) else None,
                ),
                is_error=True,
            )
        kwargs = dict(coercion.kwargs)
        if indicators_is_string and not all_indicators_requested:
            kwargs["indicators"] = indicators_raw
        if symbols_is_string:
            kwargs["symbols"] = symbols_raw

        await emit_debug_event(
            "operation_data_run.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            normalized = self._normalize_inputs(kwargs, all_indicators_requested)
        except _InvalidDataRunArgument as exc:
            await emit_debug_event(
                "operation_data_run.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        # Persist the script source once per run so the envelope can point
        # at it without echoing the body back. Failures here are non-fatal
        # but logged — the envelope still reports the in-memory metadata.
        script_persisted_path: str | None = None
        if normalized["script_source"] is not None:
            script_persisted_path = self._persist_script(normalized["script_source"])

        symbols = normalized["codes"]
        results: list[dict[str, Any]] = []
        global_meta: dict[str, Any] = {
            "requested_start": None,
            "requested_end": None,
        }

        for code in symbols:
            await emit_debug_event(
                "operation_data_run.symbol.started",
                {"tool": self.name, "code": code},
            )
            try:
                outcome = await self._run_for_symbol(code, normalized)
            except _InvalidDataRunArgument as exc:
                await emit_debug_event(
                    "operation_data_run.symbol.failed",
                    {
                        "tool": self.name,
                        "code": code,
                        "error_code": exc.error_code,
                        "error_type": exc.error_type,
                        "message": str(exc),
                    },
                )
                results.append(
                    {
                        "code": code,
                        "status": "failed",
                        "error_code": exc.error_code,
                        "error_type": exc.error_type,
                        "message": str(exc),
                        "hint": exc.hint,
                    }
                )
                continue
            except asyncio.TimeoutError:
                await emit_debug_event(
                    "operation_data_run.symbol.failed",
                    {
                        "tool": self.name,
                        "code": code,
                        "error_code": "script_timeout",
                        "timeout_seconds": normalized["script_timeout"],
                    },
                )
                results.append(
                    {
                        "code": code,
                        "status": "failed",
                        "error_code": "script_timeout",
                        "error_type": "TimeoutError",
                        "message": (
                            f"script exceeded {normalized['script_timeout']}s timeout; "
                            "worker thread may still be running"
                        ),
                        "hint": "increase script_timeout or simplify the script",
                    }
                )
                continue
            except Exception as exc:
                logger.exception("data_run unexpected failure code=%s", code)
                await emit_debug_event(
                    "operation_data_run.symbol.failed",
                    {
                        "tool": self.name,
                        "code": code,
                        "error_code": "data_run_unexpected_failure",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                results.append(
                    {
                        "code": code,
                        "status": "failed",
                        "error_code": "data_run_unexpected_failure",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                continue
            results.append(outcome)
            if global_meta["requested_start"] is None:
                global_meta["requested_start"] = outcome.get("requested_start")
                global_meta["requested_end"] = outcome.get("requested_end")
            await emit_debug_event(
                "operation_data_run.symbol.completed",
                {
                    "tool": self.name,
                    "code": code,
                    "ohlcv_path": outcome.get("ohlcv_path"),
                    "indicator_path": outcome.get("indicator_path"),
                    "indicator_columns": outcome.get("indicator_columns"),
                },
            )

        successes = [r for r in results if r.get("status") == "ok"]
        failures = [r for r in results if r.get("status") == "failed"]

        manifest_path = self._write_manifest(
            symbols=symbols,
            results=results,
            normalized=normalized,
            script_persisted_path=script_persisted_path,
        )

        payload: dict[str, Any] = {
            "status": "ok" if not failures else ("partial" if successes else "failed"),
            "interval": normalized["interval"],
            "requested_start": global_meta["requested_start"],
            "requested_end": global_meta["requested_end"],
            "warmup_bars_default": normalized["warmup_bars_default"],
            "warmup_bars_explicit": normalized["warmup_bars_explicit"],
            "script_timeout": normalized["script_timeout"],
            "symbols_total": len(symbols),
            "symbols_succeeded": len(successes),
            "symbols_failed": len(failures),
            "indicators": list(normalized["indicators"]),
            "manifest_path": manifest_path,
            "symbols": results,
            "script_source": (
                normalized["script_source"].metadata(persisted_path=script_persisted_path)
                if normalized["script_source"] is not None
                else None
            ),
        }

        await emit_debug_event(
            "operation_data_run.created",
            {
                "tool": self.name,
                "symbols_total": len(symbols),
                "symbols_succeeded": len(successes),
                "symbols_failed": len(failures),
                "manifest_path": manifest_path,
            },
        )

        header = self._summary_header(payload)
        # Per-symbol failures are reported structurally in ``symbols[]`` /
        # ``status`` — the envelope itself stays non-error so callers can
        # iterate the array without first checking is_error. Only the
        # top-level validation / kwargs-contract paths set is_error=True,
        # and those return well before this point.
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    def _normalize_inputs(
        self,
        kwargs: dict[str, Any],
        all_indicators_requested: bool,
    ) -> dict[str, Any]:
        code = kwargs.get("code")
        symbols = kwargs.get("symbols")
        universe_file = kwargs.get("universe_file")

        provided_keys = [
            name
            for name, value in (
                ("code", code),
                ("symbols", symbols),
                ("universe_file", universe_file),
            )
            if value is not None
        ]
        if len(provided_keys) == 0:
            raise _InvalidDataRunArgument(
                "missing_symbol_input",
                "pass exactly one of code / symbols / universe_file",
                "pick a single input mode",
            )
        if len(provided_keys) > 1:
            raise _InvalidDataRunArgument(
                "conflicting_symbol_args",
                f"got multiple symbol inputs: {provided_keys}",
                "pick exactly one of code / symbols / universe_file",
            )

        if code is not None:
            codes = _validate_canonical_codes([code] if not isinstance(code, list) else code)
        elif symbols is not None:
            codes = _validate_canonical_codes(_parse_csv_symbols(symbols))
        else:
            codes = _validate_canonical_codes(_load_universe_file(universe_file))

        data_source = kwargs.get("data_source") or "auto"
        if data_source not in _SUPPORTED_DATA_SOURCES:
            raise _InvalidDataRunArgument(
                "unknown_data_source",
                f"unknown data_source {data_source!r}",
                f"use one of: {', '.join(_SUPPORTED_DATA_SOURCES)}",
            )

        indicator_tool = IndicatorComputeTool()
        # data_run defaults to NO indicators when the caller didn't ask —
        # unlike ``analysis indicators`` which defaults to ALL. The
        # rationale: data_run is the omnibus orchestrator, and computing
        # 29 indicators on every fetch is rarely what the caller wants
        # when they're only here for OHLCV or a custom script. Callers
        # opt in with ``--indicators all`` or a CSV list.
        indicators_kwarg = kwargs.get("indicators")
        try:
            if indicators_kwarg is None and not all_indicators_requested:
                selected: tuple[str, ...] = ()
            else:
                selected = indicator_tool._resolve_indicators(
                    indicators_kwarg, all_indicators_requested
                )
            indicator_params = indicator_tool._resolve_params(kwargs.get("indicator_params"))
            tail = indicator_tool._resolve_tail(kwargs.get("tail"))
        except _IndicatorInvalidArgument as exc:
            raise _InvalidDataRunArgument(exc.error_code, str(exc), exc.hint) from exc

        # Script handling (validate-once-up-front per run).
        script_source = _prepare_script_source(
            script=kwargs.get("script"),
            script_file=kwargs.get("script_file"),
        )
        script_params = kwargs.get("script_params") or {}
        if not isinstance(script_params, dict):
            raise _InvalidDataRunArgument(
                "invalid_script_params_json",
                f"script_params must be an object, got {type(script_params).__name__}",
            )

        # Warmup resolution. Explicit value short-circuits; otherwise we
        # take max(built-in warmups, script REQUIRED_HISTORY).
        explicit_warmup = self._resolve_explicit_warmup(kwargs.get("warmup_bars"))
        if explicit_warmup is not None:
            warmup_default = explicit_warmup
        else:
            indicator_warmup = (
                max(
                    _estimate_indicator_warmup(name, indicator_params.get(name, {}))
                    for name in selected
                )
                if selected
                else 0
            )
            script_warmup = (
                script_source.required_history
                if script_source is not None and script_source.required_history is not None
                else 0
            )
            warmup_default = max(indicator_warmup, script_warmup)
            # If the only computation is a script with no REQUIRED_HISTORY
            # declared, refuse to silently use warmup=0 — that's the most
            # common LLM-script footgun (rolling window broken).
            if (
                script_source is not None
                and script_source.required_history is None
                and not selected
                and warmup_default == 0
            ):
                raise _InvalidDataRunArgument(
                    "script_warmup_unspecified",
                    "script provides no REQUIRED_HISTORY literal and no built-in "
                    "indicators were requested, so warmup defaulted to 0",
                    "declare REQUIRED_HISTORY = <int> at script top level, "
                    "or pass --warmup-bars N explicitly",
                )

        script_timeout = self._resolve_script_timeout(kwargs.get("script_timeout"))

        return {
            "codes": codes,
            "period": kwargs.get("period"),
            "start_date": kwargs.get("start_date"),
            "end_date": kwargs.get("end_date"),
            "interval": kwargs.get("interval") or "1d",
            "data_source": data_source,
            "indicators": selected,
            "indicator_params": indicator_params,
            "script_source": script_source,
            "script_params": script_params,
            "script_timeout": script_timeout,
            "warmup_bars_default": warmup_default,
            "warmup_bars_explicit": explicit_warmup is not None,
            "tail": tail,
        }

    def _resolve_explicit_warmup(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidDataRunArgument(
                "invalid_warmup_bars",
                f"warmup_bars must be an integer >= 0, got {type(value).__name__}({value!r})",
            )
        intval = int(value)
        if float(value) != intval or intval < 0:
            raise _InvalidDataRunArgument(
                "invalid_warmup_bars",
                f"warmup_bars must be an integer >= 0, got {value!r}",
            )
        return intval

    def _resolve_script_timeout(self, value: Any) -> float:
        if value is None:
            return self._DEFAULT_SCRIPT_TIMEOUT_SECONDS
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidDataRunArgument(
                "invalid_script_timeout",
                f"script_timeout must be a positive number, got "
                f"{type(value).__name__}({value!r})",
            )
        f = float(value)
        if f <= 0:
            raise _InvalidDataRunArgument(
                "invalid_script_timeout",
                f"script_timeout must be > 0, got {value!r}",
            )
        return f

    # ------------------------------------------------------------------
    # Per-symbol pipeline
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        market_bars_repository: Any = None,
        market_bars_provider: str | None = None,
    ) -> None:
        # Both default to None so minimal setups (and every existing test that
        # constructs ``DataRunTool()`` bare) keep the previous CSV-only
        # behaviour — warehouse warming is a no-op when either is missing.
        self._market_bars_repository = market_bars_repository
        self._market_bars_provider = (market_bars_provider or "").strip() or None

    async def _maybe_warm_market_bars(
        self,
        *,
        code: str,
        bars: list[Any],
        interval: str,
    ) -> None:
        """Mirror freshly fetched bars into the local ``market_bars`` warehouse.

        The UI's K-line panel reads ``GET /market/bars`` → the ``market_bars``
        warehouse, NOT the artifact CSV this tool writes. Without this the
        panel shows "暂无本地 K 线" even right after a successful ``data run``.
        We reuse the raw ``list[Bar]`` (canonical timestamps intact) and the
        shared ``_bar_dict`` converter so the stored rows match the worker /
        sync-range / read-through convention exactly, and we key on
        ``(provider=market_data.default_provider, adjust=qfq)`` to match what
        the read side resolves.

        Best-effort by contract: warming never fails the ``data run`` (the CSV
        artifact is already written); failures surface as a structured debug
        event + warning, not an exception.
        """
        repo = self._market_bars_repository
        provider = self._market_bars_provider
        if repo is None or not provider:
            return

        from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
        from doyoutrade.data.local_market_bars import (
            SUPPORTED_LOCAL_INTERVALS,
            _bar_dict,
        )

        if interval not in SUPPORTED_LOCAL_INTERVALS:
            await emit_debug_event(
                "operation_data_run.symbol.market_bars_skipped",
                {
                    "code": code,
                    "interval": interval,
                    "reason": "interval_not_warehouse_eligible",
                    "supported": sorted(SUPPORTED_LOCAL_INTERVALS),
                    "hint": "only 1d/5m/60m are stored in the local market_bars warehouse",
                },
            )
            return
        if not bars:
            return

        try:
            payloads = [_bar_dict(bar, interval=interval) for bar in bars]
            upserted = await repo.upsert_bars(
                provider=provider,
                adjust=DEFAULT_BAR_ADJUST,
                interval=interval,
                bars=payloads,
            )
        except Exception as exc:  # noqa: BLE001 — warming is best-effort
            logger.warning(
                "data_run market_bars warm failed code=%s interval=%s provider=%s "
                "adjust=%s error_type=%s error=%s",
                code,
                interval,
                provider,
                DEFAULT_BAR_ADJUST,
                type(exc).__name__,
                exc,
            )
            await emit_debug_event(
                "operation_data_run.symbol.market_bars_failed",
                {
                    "code": code,
                    "interval": interval,
                    "provider": provider,
                    "adjust": DEFAULT_BAR_ADJUST,
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                    "hint": "warehouse warming failed; the OHLCV artifact CSV was still "
                    "written — check repository writes and bar payload shape",
                },
            )
            return

        await emit_debug_event(
            "operation_data_run.symbol.market_bars_warmed",
            {
                "code": code,
                "interval": interval,
                "provider": provider,
                "adjust": DEFAULT_BAR_ADJUST,
                "upserted_count": int(upserted),
                "bar_count": len(payloads),
            },
        )

    async def _run_for_symbol(
        self,
        code: str,
        normalized: dict[str, Any],
    ) -> dict[str, Any]:
        market_tool = MarketDataFetcher()
        try:
            requested_start, requested_end, requested_label = market_tool._resolve_window(
                period=normalized["period"],
                start_date=normalized["start_date"],
                end_date=normalized["end_date"],
            )
        except _ConflictingRange as exc:
            raise _InvalidDataRunArgument(
                "conflicting_range_args",
                str(exc),
                "Pass either period OR start_date/end_date, not both.",
            ) from exc
        except _InvalidDate as exc:
            raise _InvalidDataRunArgument(
                "invalid_date",
                str(exc),
                "Use YYYY-MM-DD and ensure start_date <= end_date.",
            ) from exc
        except _InvalidPeriod as exc:
            raise _InvalidDataRunArgument(
                "invalid_period",
                str(exc),
                "Use <N><unit> with unit in d/w/m/mo/y, e.g. 20d or 1y.",
            ) from exc

        warmup_bars = normalized["warmup_bars_default"]
        fetch_start = self._warmup_start_date(requested_start, warmup_bars)
        period_label = (
            requested_label
            if fetch_start == requested_start
            else f"{fetch_start.isoformat()}..{requested_end.isoformat()} warmup_for {requested_label}"
        )

        try:
            fetch_df = await market_tool._fetch_ohlcv(
                code,
                start_dt=fetch_start,
                end_dt=requested_end,
                period_label=period_label,
                interval=normalized["interval"],
                data_source=normalized["data_source"],
            )
        except _IntervalNotSupportedForSymbol as exc:
            # Rejected before any network call (see market_data.py's
            # supports_interval_for_symbol pre-flight) — this is a known,
            # named constraint (e.g. index + minute interval on baostock),
            # not an upstream failure, so it gets its own error_code instead
            # of being folded into the generic data_fetch_failed bucket.
            logger.info(
                "data_run interval unsupported code=%s data_source=%s interval=%s",
                code,
                normalized["data_source"],
                normalized["interval"],
            )
            raise _InvalidDataRunArgument(
                "interval_not_supported_for_instrument_type",
                str(exc),
                "use --interval 1d for indices, or try a different --data-source",
                error_type=type(exc).__name__,
            ) from exc
        except Exception as exc:
            logger.warning(
                "data_run fetch failed code=%s data_source=%s interval=%s err=%s",
                code,
                normalized["data_source"],
                normalized["interval"],
                exc,
            )
            raise _InvalidDataRunArgument(
                "data_fetch_failed",
                f"failed to fetch OHLCV for {code}: {exc}",
                "check the symbol and data_source",
                error_type=type(exc).__name__,
            ) from exc

        fetch_df = self._normalize_ohlcv_frame(fetch_df)
        target_df = self._target_window(fetch_df, requested_start, requested_end)

        await emit_debug_event(
            "operation_data_run.symbol.validated",
            {
                "tool": self.name,
                "code": code,
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "fetch_start": fetch_start.isoformat(),
                "warmup_bars": warmup_bars,
                "indicators": list(normalized["indicators"]),
                "has_script": normalized["script_source"] is not None,
            },
        )

        series_map, latest = await self._compute_outputs(
            fetch_df=fetch_df,
            target_df=target_df,
            normalized=normalized,
            code=code,
        )

        # Persist artifacts.
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        safe = _safe_code(code)
        ohlcv_path = root / f"ohlcv_{safe}.csv"
        target_df[list(_REQUIRED_COLUMNS)].to_csv(ohlcv_path, index=True, index_label="date")
        indicator_path: str | None = None
        if series_map:
            out = pd.DataFrame(series_map, index=target_df.index)
            indicator_csv = root / f"data_run_indicators_{safe}.csv"
            out.to_csv(indicator_csv, index=True, index_label="date")
            indicator_path = str(indicator_csv)

        # Warm the local market_bars warehouse from the raw fetched bars so the
        # UI K-line panel (GET /market/bars) renders without a separate sync.
        await self._maybe_warm_market_bars(
            code=code,
            bars=market_tool._last_bars,
            interval=normalized["interval"],
        )

        return {
            "code": code,
            "status": "ok",
            "data_source": (
                market_tool._last_used_source
                if normalized["data_source"] == "auto"
                else normalized["data_source"]
            ),
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "fetch_start": fetch_start.isoformat(),
            "fetch_end": requested_end.isoformat(),
            "warmup_bars": warmup_bars,
            "input_rows": int(len(fetch_df)),
            "ohlcv_rows": int(len(target_df)),
            "ohlcv_path": str(ohlcv_path),
            "indicator_path": indicator_path,
            "indicator_columns": list(series_map.keys()),
            "latest": latest,
        }

    async def _compute_outputs(
        self,
        *,
        fetch_df: pd.DataFrame,
        target_df: pd.DataFrame,
        normalized: dict[str, Any],
        code: str,
    ) -> tuple[dict[str, pd.Series], dict[str, Any]]:
        series_map: dict[str, pd.Series] = {}
        latest: dict[str, Any] = {}
        indicators = normalized["indicators"]
        if indicators:
            indicator_params = dict(normalized["indicator_params"])
            for board_name in ("limit_up_approx", "limit_down_approx"):
                if board_name in indicators:
                    board_params = dict(indicator_params.get(board_name) or {})
                    board_params.setdefault("symbol", code)
                    indicator_params[board_name] = board_params
            try:
                built_series, _ = IndicatorComputeTool()._compute(
                    fetch_df,
                    indicators,
                    indicator_params,
                    normalized["tail"],
                )
            except _IndicatorInvalidArgument as exc:
                raise _InvalidDataRunArgument(exc.error_code, str(exc), exc.hint) from exc
            for column, series in built_series.items():
                aligned = series.reindex(target_df.index)
                series_map[column] = aligned
                latest[column] = _tail_values(aligned, normalized["tail"])

        script_source = normalized["script_source"]
        if script_source is not None:
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(
                        _exec_script_sync,
                        source=script_source,
                        fetch_df=fetch_df,
                        target_df=target_df,
                        params=normalized["script_params"],
                    ),
                    timeout=normalized["script_timeout"],
                )
            except asyncio.TimeoutError:
                raise  # bubble up to the per-symbol catch
            except _InvalidDataRunArgument:
                raise
            custom = _coerce_script_result(raw, target_df.index)
            for column, series in custom.items():
                final_column = f"custom.{column}"
                aligned = series.reindex(target_df.index)
                series_map[final_column] = aligned
                latest[final_column] = _tail_values(aligned, normalized["tail"])

        return series_map, latest

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _warmup_start_date(self, requested_start: date, warmup_bars: int) -> date:
        if warmup_bars <= 0:
            return requested_start
        calendar_days = int(math.ceil(warmup_bars * 7 / 5)) + 7
        return requested_start - timedelta(days=calendar_days)

    def _normalize_ohlcv_frame(self, df: Any) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame):
            raise _InvalidDataRunArgument(
                "ohlcv_frame_invalid",
                f"fetch returned {type(df).__name__}, expected pandas.DataFrame",
            )
        if df.empty:
            raise _InvalidDataRunArgument("ohlcv_empty", "fetch returned no bars")
        missing = [col for col in _REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            raise _InvalidDataRunArgument(
                "ohlcv_columns_missing",
                f"OHLCV data is missing columns: {missing}",
            )
        out = df.copy()
        out.index = pd.to_datetime(out.index)
        return out.sort_index()

    def _target_window(self, df: pd.DataFrame, start_dt: date, end_dt: date) -> pd.DataFrame:
        # ``_normalize_ohlcv_frame`` coerced the index to datetime, so we can
        # compare against bar timestamps directly. The end timestamp is
        # extended to the end of the requested day so bars timestamped
        # mid-session still fall in range.
        start_ts = pd.Timestamp(start_dt)
        end_ts = pd.Timestamp(end_dt) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        mask = (df.index >= start_ts) & (df.index <= end_ts)
        target = df.loc[mask].copy()
        if target.empty:
            raise _InvalidDataRunArgument(
                "no_ohlcv_in_requested_window",
                f"no bars found in requested window {start_dt.isoformat()}..{end_dt.isoformat()}",
                "check the date range and provider trading calendar",
            )
        return target

    def _persist_script(self, source: _ScriptSource) -> str | None:
        """Write the validated script body next to the OHLCV artifacts.

        The envelope returns the path so debug exports can read the exact
        source that ran without keeping it inline in every payload.
        """

        try:
            root = _get_artifacts_root()
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"data_run_script_{source.sha256}.py"
            if not path.exists():
                path.write_text(source.code, encoding="utf-8")
            return str(path)
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning(
                "data_run failed to persist script body sha=%s err=%s",
                source.sha256, exc,
            )
            return None

    def _write_manifest(
        self,
        *,
        symbols: list[str],
        results: list[dict[str, Any]],
        normalized: dict[str, Any],
        script_persisted_path: str | None,
    ) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        # Use a digest of the requested symbol set + window so successive
        # calls overwrite an existing manifest rather than littering.
        manifest_key = hashlib.sha256(
            json.dumps(
                {
                    "symbols": symbols,
                    "period": normalized["period"],
                    "start_date": normalized["start_date"],
                    "end_date": normalized["end_date"],
                    "interval": normalized["interval"],
                    "indicators": list(normalized["indicators"]),
                    "script_sha256": (
                        normalized["script_source"].sha256
                        if normalized["script_source"] is not None
                        else None
                    ),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        manifest_path = root / f"data_run_manifest_{manifest_key}.json"
        manifest = {
            "symbols_total": len(symbols),
            "symbols": results,
            "script_source": (
                normalized["script_source"].metadata(persisted_path=script_persisted_path)
                if normalized["script_source"] is not None
                else None
            ),
            "indicators": list(normalized["indicators"]),
            "indicator_params": normalized["indicator_params"],
            "warmup_bars_default": normalized["warmup_bars_default"],
            "warmup_bars_explicit": normalized["warmup_bars_explicit"],
            "interval": normalized["interval"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return str(manifest_path)

    def _summary_header(self, payload: dict[str, Any]) -> str:
        total = payload["symbols_total"]
        succ = payload["symbols_succeeded"]
        fail = payload["symbols_failed"]
        ind_count = len(payload["indicators"])
        bits = [
            f"data_run: {succ}/{total} symbols ok",
        ]
        if fail:
            bits.append(f"{fail} failed")
        if payload["requested_start"] and payload["requested_end"]:
            bits.append(f"window {payload['requested_start']}->{payload['requested_end']}")
        bits.append(f"warmup={payload['warmup_bars_default']}")
        bits.append(f"indicators={ind_count}")
        return "; ".join(bits) + "."


__all__ = ["DataRunTool"]
