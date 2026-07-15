"""Tunable strategy parameters with declared hyperopt search spaces.

Each parameter is a class attribute on :class:`Strategy`::

    class MyStrategy(Strategy):
        fast = IntParameter(5, 15, default=10, optimize=True)
        threshold = DecimalParameter(0.01, 0.10, default=0.05, decimals=3)
        mode = CategoricalParameter(["aggressive", "moderate"], default="moderate")

Strategy code reads the current value via ``self.fast.value`` (NOT
``self.fast`` — that's the Parameter descriptor itself, exposing the search
space). The runner binds ``.value`` at strategy instantiation: an explicit
override from cycle parameters wins, otherwise ``default`` is used.

Why a descriptor instead of a dataclass field:

- ``StrategyCompiler`` extracts the search space at compile time without
  instantiating the strategy (parameters are class-level metadata).
- Hyperopt enumerates ``optimize=True`` parameters; ``optimize=False``
  parameters are tunable constants but not searched.
- The same parameter object is shared across cycle invocations, so reading
  ``.value`` is O(1) and the search space declaration is single-sourced.
"""

from __future__ import annotations

import decimal
import numbers
from dataclasses import dataclass, field
from typing import Any, Generic, Sequence, TypeVar

from doyoutrade.strategy_sdk.errors import (
    INVALID_ARGUMENT,
    StrategyValidationError,
)

T = TypeVar("T")


@dataclass
class _ParameterBase(Generic[T]):
    """Base for all parameter types. Subclasses set ``_typename`` for errors."""

    default: T
    optimize: bool = True
    description: str = ""
    # Runtime-bound value. None means "use default". Bound by the runner
    # before invoking strategy methods.
    _bound_value: T | None = field(default=None, init=False, repr=False)

    _typename: str = field(default="parameter", init=False, repr=False)

    @property
    def value(self) -> T:
        """The currently bound value, or :attr:`default` if not overridden."""
        if self._bound_value is None:
            return self.default
        return self._bound_value

    def bind(self, value: T | None) -> None:
        """Override ``.value`` for the current strategy instance.

        Called by :class:`StrategyRunner` once per cycle from the cycle's
        parameter mapping. ``None`` resets to the declared default.
        """
        if value is None:
            self._bound_value = None
            return
        self._bound_value = self._coerce(value)

    def search_space(self) -> dict[str, Any]:
        """JSON-serializable description of this parameter's search space.

        Persisted into ``strategy_definitions.parameter_schema_json`` and
        consumed by hyperopt + the UI's parameter editor.
        """
        raise NotImplementedError

    def _coerce(self, value: Any) -> T:
        raise NotImplementedError(
            f"{self._typename}._coerce must be implemented by subclass (value={value!r})"
        )


@dataclass
class IntParameter(_ParameterBase[int]):
    """Integer parameter with inclusive ``[low, high]`` search range."""

    low: int = 0
    high: int = 0
    step: int = 1

    def __init__(
        self,
        low: int,
        high: int,
        *,
        default: int | None = None,
        step: int = 1,
        optimize: bool = True,
        description: str = "",
    ) -> None:
        if not isinstance(low, numbers.Integral) or not isinstance(high, numbers.Integral):
            raise StrategyValidationError(
                f"IntParameter low/high must be int, got low={low!r} high={high!r}",
                error_code=INVALID_ARGUMENT,
                hint="IntParameter(5, 20, default=10)",
            )
        if low > high:
            raise StrategyValidationError(
                f"IntParameter low={low} > high={high}",
                error_code=INVALID_ARGUMENT,
            )
        if default is None:
            default = int(low)
        elif not isinstance(default, numbers.Integral):
            raise StrategyValidationError(
                f"IntParameter default must be int, got {default!r}",
                error_code=INVALID_ARGUMENT,
            )
        if not (low <= int(default) <= high):
            raise StrategyValidationError(
                f"IntParameter default={default} outside [{low}, {high}]",
                error_code=INVALID_ARGUMENT,
            )
        super().__init__(default=int(default), optimize=optimize, description=description)
        self.low = int(low)
        self.high = int(high)
        self.step = int(step)
        self._typename = "IntParameter"

    def search_space(self) -> dict[str, Any]:
        return {
            "type": "int",
            "low": self.low,
            "high": self.high,
            "step": self.step,
            "default": self.default,
            "optimize": self.optimize,
            "description": self.description,
        }

    def _coerce(self, value: Any) -> int:
        if isinstance(value, bool):
            raise StrategyValidationError(
                f"IntParameter rejects bool ({value!r}); use IntParameter only for integers.",
                error_code=INVALID_ARGUMENT,
            )
        if not isinstance(value, numbers.Integral):
            raise StrategyValidationError(
                f"IntParameter bind expects int, got {type(value).__name__}={value!r}",
                error_code=INVALID_ARGUMENT,
            )
        v = int(value)
        if not (self.low <= v <= self.high):
            raise StrategyValidationError(
                f"IntParameter bind value {v} outside [{self.low}, {self.high}]",
                error_code=INVALID_ARGUMENT,
            )
        return v


@dataclass
class DecimalParameter(_ParameterBase[float]):
    """Fixed-precision float parameter (decimals controls hyperopt grid size)."""

    low: float = 0.0
    high: float = 0.0
    decimals: int = 3

    def __init__(
        self,
        low: float,
        high: float,
        *,
        default: float | None = None,
        decimals: int = 3,
        optimize: bool = True,
        description: str = "",
    ) -> None:
        if not isinstance(low, numbers.Real) or not isinstance(high, numbers.Real):
            raise StrategyValidationError(
                f"DecimalParameter low/high must be numeric, got low={low!r} high={high!r}",
                error_code=INVALID_ARGUMENT,
            )
        low_f, high_f = float(low), float(high)
        if low_f > high_f:
            raise StrategyValidationError(
                f"DecimalParameter low={low_f} > high={high_f}",
                error_code=INVALID_ARGUMENT,
            )
        if decimals < 0:
            raise StrategyValidationError(
                f"DecimalParameter decimals must be >= 0, got {decimals}",
                error_code=INVALID_ARGUMENT,
            )
        if default is None:
            default = low_f
        elif not isinstance(default, numbers.Real):
            raise StrategyValidationError(
                f"DecimalParameter default must be numeric, got {default!r}",
                error_code=INVALID_ARGUMENT,
            )
        default_f = round(float(default), decimals)
        if not (low_f <= default_f <= high_f):
            raise StrategyValidationError(
                f"DecimalParameter default={default_f} outside [{low_f}, {high_f}]",
                error_code=INVALID_ARGUMENT,
            )
        super().__init__(default=default_f, optimize=optimize, description=description)
        self.low = low_f
        self.high = high_f
        self.decimals = int(decimals)
        self._typename = "DecimalParameter"

    def search_space(self) -> dict[str, Any]:
        return {
            "type": "decimal",
            "low": self.low,
            "high": self.high,
            "decimals": self.decimals,
            "default": self.default,
            "optimize": self.optimize,
            "description": self.description,
        }

    def _coerce(self, value: Any) -> float:
        if isinstance(value, bool):
            raise StrategyValidationError(
                f"DecimalParameter rejects bool ({value!r})",
                error_code=INVALID_ARGUMENT,
            )
        if isinstance(value, decimal.Decimal):
            v = float(value)
        elif isinstance(value, numbers.Real):
            v = float(value)
        else:
            raise StrategyValidationError(
                f"DecimalParameter bind expects numeric, got {type(value).__name__}={value!r}",
                error_code=INVALID_ARGUMENT,
            )
        v = round(v, self.decimals)
        if not (self.low <= v <= self.high):
            raise StrategyValidationError(
                f"DecimalParameter bind value {v} outside [{self.low}, {self.high}]",
                error_code=INVALID_ARGUMENT,
            )
        return v


@dataclass
class CategoricalParameter(_ParameterBase[Any]):
    """Discrete-choice parameter from a fixed set of values."""

    choices: tuple[Any, ...] = ()

    def __init__(
        self,
        choices: Sequence[Any],
        *,
        default: Any = None,
        optimize: bool = True,
        description: str = "",
    ) -> None:
        choices_tuple = tuple(choices)
        if len(choices_tuple) == 0:
            raise StrategyValidationError(
                "CategoricalParameter requires at least one choice",
                error_code=INVALID_ARGUMENT,
            )
        if default is None:
            default = choices_tuple[0]
        if default not in choices_tuple:
            raise StrategyValidationError(
                f"CategoricalParameter default={default!r} not in choices={choices_tuple!r}",
                error_code=INVALID_ARGUMENT,
            )
        super().__init__(default=default, optimize=optimize, description=description)
        self.choices = choices_tuple
        self._typename = "CategoricalParameter"

    def search_space(self) -> dict[str, Any]:
        return {
            "type": "categorical",
            "choices": list(self.choices),
            "default": self.default,
            "optimize": self.optimize,
            "description": self.description,
        }

    def _coerce(self, value: Any) -> Any:
        if value not in self.choices:
            raise StrategyValidationError(
                f"CategoricalParameter bind value {value!r} not in choices {self.choices!r}",
                error_code=INVALID_ARGUMENT,
            )
        return value


@dataclass
class BooleanParameter(_ParameterBase[bool]):
    """Two-value parameter (essentially a sugar over CategoricalParameter)."""

    def __init__(
        self,
        *,
        default: Any = False,
        optimize: bool = True,
        description: str = "",
    ) -> None:
        if not isinstance(default, bool):
            raise StrategyValidationError(
                f"BooleanParameter default must be bool, got {default!r}",
                error_code=INVALID_ARGUMENT,
            )
        super().__init__(default=default, optimize=optimize, description=description)
        self._typename = "BooleanParameter"

    def search_space(self) -> dict[str, Any]:
        return {
            "type": "boolean",
            "default": self.default,
            "optimize": self.optimize,
            "description": self.description,
        }

    def _coerce(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise StrategyValidationError(
                f"BooleanParameter bind expects bool, got {type(value).__name__}={value!r}",
                error_code=INVALID_ARGUMENT,
            )
        return value


def collect_parameters(strategy_cls: type) -> dict[str, _ParameterBase[Any]]:
    """Walk a Strategy subclass's MRO and return its declared parameters.

    Subclass declarations override base-class ones with the same attribute
    name (Python MRO semantics already give us this — we just expose the
    mapping). Used by the compiler to extract the search space and by the
    runner to bind cycle-supplied overrides.
    """
    out: dict[str, _ParameterBase[Any]] = {}
    # Walk in reverse MRO so subclass declarations override base ones.
    for cls in reversed(strategy_cls.__mro__):
        for name, attr in vars(cls).items():
            if isinstance(attr, _ParameterBase):
                out[name] = attr
    return out


__all__ = [
    "BooleanParameter",
    "CategoricalParameter",
    "DecimalParameter",
    "IntParameter",
    "collect_parameters",
]
