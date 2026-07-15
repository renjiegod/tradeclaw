"""Validate the JSON blob for ``model_route.settings``.

``settings`` is a single override layer mapped over
:func:`doyoutrade.config.default_model_route_baseline` at resolution time and then
into :class:`~doyoutrade.config.ModelSettings` and the provider-side dataclasses
(see :func:`doyoutrade.models.route_resolution.resolve_model_settings`,
``_anthropic_settings_from_flat_mapping``, ``_openai_compatible_settings_from_flat_mapping``
in ``doyoutrade.config``).

**Top-level allowlist only:** keys allowed here are exactly those that may appear at the
**root** of that patch: route scalars (``temperature``, ``max_tokens``, ``timeout_seconds``,
``signal_strategy``) plus provider-style keys (``api_key``, ``base_url``, ``thinking``,
``cache_control`` for Anthropic; ``api_key``, ``base_url``, ``tool_choice``, ``max_tokens``,
``prediction_config_extra`` for OpenAI-compatible / LM Studio). Nested structures under
``thinking`` / ``cache_control`` / ``prediction_config_extra`` are **not** recursively
allowlisted here; shape checks remain the responsibility of the existing parsers at
resolution time.

The validator returns a new ``dict`` (possibly empty). ``None`` / missing maps to ``{}``.
Unknown top-level keys raise :class:`ValueError` with a stable path prefix for operators.
"""

from __future__ import annotations

from typing import Any

# Mirrors keys reachable at the root of the settings patch (see module docstring).
_SETTINGS_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "temperature",
        "max_tokens",
        "timeout_seconds",
        "signal_strategy",
        "api_key",
        "base_url",
        "thinking",
        "cache_control",
        "tool_choice",
        "prediction_config_extra",
    }
)

_JSON_PATH_ROUTE_SETTINGS = "model_route.settings"


def _coerce_mapping(obj: object, *, json_path: str) -> dict[str, Any]:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError(f"{json_path}: expected a JSON object (mapping) or null, got {type(obj).__name__}")
    return dict(obj)


def _reject_unknown_top_level_keys(raw: dict[str, Any], *, json_path: str) -> dict[str, Any]:
    unknown = sorted(k for k in raw if k not in _SETTINGS_TOP_LEVEL_KEYS)
    if unknown:
        joined = ", ".join(repr(k) for k in unknown)
        raise ValueError(f"{json_path}: unknown top-level key(s): {joined}")
    return dict(raw)


def validate_route_settings(obj: object) -> dict[str, Any]:
    """Return a copy of ``obj`` with only allowlisted top-level keys (route ``settings``)."""
    raw = _coerce_mapping(obj, json_path=_JSON_PATH_ROUTE_SETTINGS)
    return _reject_unknown_top_level_keys(raw, json_path=_JSON_PATH_ROUTE_SETTINGS)
