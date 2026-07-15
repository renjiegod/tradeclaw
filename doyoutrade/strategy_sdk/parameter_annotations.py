"""``# @param`` annotation parser — alternate way to declare Strategy parameters.

Inspired by QuantDinger's ``# @param`` comment syntax. Lets agents /
authors declare tunable parameters with a single-line comment instead of
the class-attribute ``IntParameter(...)`` descriptor:

::

    # @param fast int default=10 range=5,15
    # @param threshold decimal default=0.03 range=0.01,0.1 decimals=3
    # @param mode categorical default=moderate choices=aggressive,moderate
    # @param use_trailing bool default=false

    class MyStrategy(Strategy):
        timeframe = "1d"
        startup_history = 30

        def on_bar(self, df, ctx):
            if self.fast.value > 10 and self.threshold.value > 0.02:
                return Signal.buy(tag="...")
            return Signal.hold()

The parser scans the raw source text (comments are stripped by
``ast.parse``, so we can't use AST here) and returns
``{name: ParameterBase}`` ready to inject as class attributes.

The :class:`StrategyCompiler` calls :func:`apply_parameter_annotations`
on the compiled strategy class so authored code can mix-and-match:
descriptors declared as class attributes win when there's a collision —
the comment form only fills in parameters not already declared.

Grammar (one line, leading whitespace allowed, anything after the body
is treated as description for the descriptor):

::

    # @param <name> int  [default=N]  [range=lo,hi[,step]]  [optimize=true|false]
    # @param <name> decimal  [default=N]  [range=lo,hi]  [decimals=N]  [optimize=...]
    # @param <name> categorical  [default=X]  choices=a,b,c  [optimize=...]
    # @param <name> bool  [default=true|false]  [optimize=...]

``optimize`` defaults to ``true`` (same as the IntParameter constructor
default). For ``int`` / ``decimal`` the ``range`` is mandatory unless
the type is ``categorical`` (in which case ``choices`` is mandatory) or
``bool`` (no range needed).
"""

from __future__ import annotations

import re
from typing import Any

from doyoutrade.strategy_sdk.errors import (
    INVALID_ARGUMENT,
    StrategyValidationError,
)
from doyoutrade.strategy_sdk.parameters import (
    BooleanParameter,
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
    _ParameterBase,
)


# One annotation line. Captures (name, type, body).
_ANNOTATION_RE = re.compile(
    r"^\s*#\s*@param\s+(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s+(?P<type>\w+)\s*(?P<body>.*?)\s*$"
)

# key=value pairs in the body. value may contain commas (for range / choices)
# but not whitespace; descriptions trail any unrecognized "word" tokens.
_KV_RE = re.compile(r"(?P<key>\w+)\s*=\s*(?P<val>\S+)")


_BOOL_TRUE = frozenset({"true", "1", "yes", "y", "t"})
_BOOL_FALSE = frozenset({"false", "0", "no", "n", "f"})


def _parse_bool(raw: str, *, field: str) -> bool:
    s = raw.strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    raise StrategyValidationError(
        f"@param: {field}={raw!r} is not a boolean (use true/false)",
        error_code=INVALID_ARGUMENT,
    )


def _parse_optimize(kwargs: dict[str, str]) -> bool:
    raw = kwargs.pop("optimize", None)
    if raw is None:
        return True
    return _parse_bool(raw, field="optimize")


def _parse_description(body: str, _kwargs: dict[str, str]) -> str:
    """Reconstruct any free-text description trailing the key=value pairs.

    We greedily match ``key=value`` from the body; anything not consumed
    (and not a bare ``=`` token) becomes the description. ``_kwargs`` is
    accepted for symmetry with the builder signatures but not read here
    — the regex pass over ``body`` is enough.
    """
    cleaned = _KV_RE.sub("", body)
    return " ".join(tok for tok in cleaned.split() if tok)


def _build_int(name: str, kwargs: dict[str, str], description: str) -> IntParameter:
    range_raw = kwargs.pop("range", None)
    if not range_raw:
        raise StrategyValidationError(
            f"@param {name}: int requires range=lo,hi[,step]",
            error_code=INVALID_ARGUMENT,
        )
    parts = [p.strip() for p in range_raw.split(",") if p.strip()]
    if len(parts) not in (2, 3):
        raise StrategyValidationError(
            f"@param {name}: range must be lo,hi or lo,hi,step (got {range_raw!r})",
            error_code=INVALID_ARGUMENT,
        )
    try:
        low = int(parts[0])
        high = int(parts[1])
        step = int(parts[2]) if len(parts) == 3 else 1
    except ValueError as e:
        raise StrategyValidationError(
            f"@param {name}: range parts must be ints (got {range_raw!r})",
            error_code=INVALID_ARGUMENT,
        ) from e
    default_raw = kwargs.pop("default", None)
    default: int | None
    if default_raw is None:
        default = None
    else:
        try:
            default = int(default_raw)
        except ValueError as e:
            raise StrategyValidationError(
                f"@param {name}: default must be int (got {default_raw!r})",
                error_code=INVALID_ARGUMENT,
            ) from e
    optimize = _parse_optimize(kwargs)
    _warn_unknown_kwargs(name, kwargs, ("range", "default", "optimize"))
    return IntParameter(
        low, high, default=default, step=step, optimize=optimize, description=description
    )


def _build_decimal(name: str, kwargs: dict[str, str], description: str) -> DecimalParameter:
    range_raw = kwargs.pop("range", None)
    if not range_raw:
        raise StrategyValidationError(
            f"@param {name}: decimal requires range=lo,hi",
            error_code=INVALID_ARGUMENT,
        )
    parts = [p.strip() for p in range_raw.split(",") if p.strip()]
    if len(parts) != 2:
        raise StrategyValidationError(
            f"@param {name}: decimal range must be lo,hi (got {range_raw!r})",
            error_code=INVALID_ARGUMENT,
        )
    try:
        low = float(parts[0])
        high = float(parts[1])
    except ValueError as e:
        raise StrategyValidationError(
            f"@param {name}: range parts must be numeric (got {range_raw!r})",
            error_code=INVALID_ARGUMENT,
        ) from e
    default_raw = kwargs.pop("default", None)
    default: float | None
    if default_raw is None:
        default = None
    else:
        try:
            default = float(default_raw)
        except ValueError as e:
            raise StrategyValidationError(
                f"@param {name}: default must be numeric (got {default_raw!r})",
                error_code=INVALID_ARGUMENT,
            ) from e
    decimals_raw = kwargs.pop("decimals", None)
    if decimals_raw is None:
        decimals = 3
    else:
        try:
            decimals = int(decimals_raw)
        except ValueError as e:
            raise StrategyValidationError(
                f"@param {name}: decimals must be int (got {decimals_raw!r})",
                error_code=INVALID_ARGUMENT,
            ) from e
    optimize = _parse_optimize(kwargs)
    _warn_unknown_kwargs(name, kwargs, ("range", "default", "decimals", "optimize"))
    return DecimalParameter(
        low, high, default=default, decimals=decimals, optimize=optimize, description=description
    )


def _build_categorical(
    name: str, kwargs: dict[str, str], description: str
) -> CategoricalParameter:
    choices_raw = kwargs.pop("choices", None)
    if not choices_raw:
        raise StrategyValidationError(
            f"@param {name}: categorical requires choices=a,b,c",
            error_code=INVALID_ARGUMENT,
        )
    choices = [c.strip() for c in choices_raw.split(",") if c.strip()]
    if not choices:
        raise StrategyValidationError(
            f"@param {name}: choices must contain at least one value",
            error_code=INVALID_ARGUMENT,
        )
    default = kwargs.pop("default", None)
    if default is not None:
        default = default.strip()
        if default not in choices:
            raise StrategyValidationError(
                f"@param {name}: default={default!r} not in choices {choices!r}",
                error_code=INVALID_ARGUMENT,
            )
    optimize = _parse_optimize(kwargs)
    _warn_unknown_kwargs(name, kwargs, ("choices", "default", "optimize"))
    return CategoricalParameter(
        choices, default=default, optimize=optimize, description=description
    )


def _build_bool(name: str, kwargs: dict[str, str], description: str) -> BooleanParameter:
    default_raw = kwargs.pop("default", "false")
    default = _parse_bool(default_raw, field=f"{name}.default")
    optimize = _parse_optimize(kwargs)
    _warn_unknown_kwargs(name, kwargs, ("default", "optimize"))
    return BooleanParameter(default=default, optimize=optimize, description=description)


def _warn_unknown_kwargs(name: str, kwargs: dict[str, str], allowed: tuple[str, ...]) -> None:
    leftover = set(kwargs.keys()) - set(allowed)
    if leftover:
        raise StrategyValidationError(
            f"@param {name}: unknown keys {sorted(leftover)!r}; allowed: {sorted(allowed)!r}",
            error_code=INVALID_ARGUMENT,
        )


_TYPE_BUILDERS = {
    "int": _build_int,
    "decimal": _build_decimal,
    "float": _build_decimal,  # alias
    "categorical": _build_categorical,
    "choice": _build_categorical,  # alias
    "bool": _build_bool,
    "boolean": _build_bool,
}


def parse_parameter_annotations(source_code: str) -> dict[str, _ParameterBase[Any]]:
    """Scan ``source_code`` for ``# @param`` annotation comments.

    Returns a ``{name: ParameterBase}`` mapping. Lines that don't match
    the annotation pattern are silently ignored. Lines that match but
    have a malformed body raise :class:`StrategyValidationError` with
    ``error_code='invalid_argument'`` — the agent gets a clear repair
    target instead of a silently-dropped parameter.
    """
    out: dict[str, _ParameterBase[Any]] = {}
    for line in source_code.splitlines():
        m = _ANNOTATION_RE.match(line)
        if m is None:
            continue
        name = m.group("name")
        type_key = m.group("type").lower()
        body = m.group("body") or ""

        builder = _TYPE_BUILDERS.get(type_key)
        if builder is None:
            raise StrategyValidationError(
                f"@param {name}: unknown type {type_key!r} "
                f"(allowed: int / decimal / categorical / bool)",
                error_code=INVALID_ARGUMENT,
            )

        kwargs: dict[str, str] = {}
        for kv in _KV_RE.finditer(body):
            kwargs[kv.group("key")] = kv.group("val")
        description = _parse_description(body, kwargs)
        if name in out:
            raise StrategyValidationError(
                f"@param {name}: duplicate annotation",
                error_code=INVALID_ARGUMENT,
            )
        out[name] = builder(name, kwargs, description)
    return out


def apply_parameter_annotations(
    strategy_class: type, source_code: str
) -> dict[str, _ParameterBase[Any]]:
    """Parse annotations from ``source_code`` and inject any that don't
    collide with already-declared class attributes.

    Returns the mapping of injected parameters (subset of the parsed set —
    parameters with the same name as an existing class attribute are
    skipped, so users can declare via ``IntParameter(...)`` class
    attribute and override / supplement via ``# @param`` — the class
    attribute always wins, since it's the more explicit form).
    """
    parsed = parse_parameter_annotations(source_code)
    injected: dict[str, _ParameterBase[Any]] = {}
    for name, param in parsed.items():
        if name in vars(strategy_class):
            continue
        setattr(strategy_class, name, param)
        injected[name] = param
    return injected


__all__ = [
    "apply_parameter_annotations",
    "parse_parameter_annotations",
]
