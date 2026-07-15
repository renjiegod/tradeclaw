"""Shared metadata containers exposed by the strategy SDK.

After the Strategy refactor, strategies receive market data directly via
the ``data_map: dict[str, pandas.DataFrame]`` argument to
:meth:`Strategy.generate`; the legacy TypedDicts that described
``StrategyContext.data`` / ``ctx.portfolio`` / ``ctx.ohlcv(symbol)`` have been
removed along with the legacy SDK.

The only type that survives is :class:`StrategyDescriptor`, which is still
optionally returned by ``Strategy.describe()`` for the strategy registry
to display the parameter schema and capabilities in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_mapping(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if mapping is None:
        return MappingProxyType({})
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            out[key] = _freeze_mapping(value)
        elif isinstance(value, list | tuple):
            out[key] = tuple(
                _freeze_mapping(item) if isinstance(item, Mapping) else item
                for item in value
            )
        else:
            out[key] = value
    return MappingProxyType(out)


def _thaw_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-copy a (possibly frozen) mapping into plain ``dict``/``list``.

    Used at persistence/serialization boundaries where ``MappingProxyType``
    leaks out of :func:`_freeze_mapping` would break JSON encoding.
    """
    if mapping is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        out[key] = _thaw_value(value)
    return out


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, list | tuple):
        return [_thaw_value(item) for item in value]
    return value


@dataclass(frozen=True)
class StrategyDescriptor:
    """Optional metadata returned from ``Strategy.describe()``.

    Consumed by :class:`StrategyCompiler` and persisted into the strategy
    registry (``parameter_schema_json`` column). ``parameter_schema`` is
    *informational* — the runtime does not validate ``ctx.parameters``
    against it.
    """

    name: str
    description: str = ""
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_schema", _freeze_mapping(self.parameter_schema))
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))


__all__ = ["StrategyDescriptor"]
