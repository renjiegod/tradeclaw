"""Resolve :class:`~doyoutrade.config.ModelSettings` from a DB ``model_route`` + code baseline defaults."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from doyoutrade.config import (
    ModelSettings,
    _anthropic_settings_from_flat_mapping,
    _inactive_anthropic_settings,
    _inactive_lmstudio_settings,
    _inactive_openai_compatible_settings,
    _lmstudio_settings_from_flat_mapping,
    _maybe_str,
    _openai_compatible_settings_from_flat_mapping,
    _parse_optional_model_max_tokens,
    _parse_signal_strategy,
    _resolve_secret,
    default_model_route_baseline,
)
from doyoutrade.models.route_settings_validate import validate_route_settings

if TYPE_CHECKING:
    from doyoutrade.persistence.repositories import SqlAlchemyModelRouteRepository

_SCALAR_PATCH_KEYS = frozenset({"temperature", "max_tokens", "timeout_seconds", "signal_strategy"})


async def resolve_model_settings(
    *,
    route_name: str,
    route_repository: SqlAlchemyModelRouteRepository,
) -> ModelSettings:
    """Load the merged model route, layer validated JSON ``settings`` over
    :func:`default_model_route_baseline`.

    The route is now self-contained (connection + credentials + overrides live on
    the single ``model_routes`` row), so there is only one JSON patch layer.
    ``RecordNotFoundError`` from the repository is not caught here.
    """
    route = await route_repository.get_by_route_name(route_name)

    baseline = default_model_route_baseline()
    patch = validate_route_settings(route.settings)

    temperature = float(patch["temperature"]) if "temperature" in patch else baseline.temperature
    max_tokens = (
        _parse_optional_model_max_tokens(patch["max_tokens"])
        if "max_tokens" in patch
        else baseline.max_tokens
    )
    timeout_seconds = (
        float(patch["timeout_seconds"]) if "timeout_seconds" in patch else baseline.timeout_seconds
    )
    signal_strategy = (
        _parse_signal_strategy(patch["signal_strategy"])
        if "signal_strategy" in patch
        else baseline.signal_strategy
    )

    rest = {k: v for k, v in patch.items() if k not in _SCALAR_PATCH_KEYS}
    provider_kind = route.provider_kind.strip().lower()
    if provider_kind not in ("anthropic", "openai_compatible", "lmstudio"):
        raise ValueError(
            f"model route {route_name!r} has invalid provider_kind "
            f"{route.provider_kind!r}; expected 'anthropic', 'openai_compatible', or 'lmstudio'"
        )

    if provider_kind == "openai_compatible":
        if _maybe_str(route.base_url) is None:
            raise ValueError(
                f"openai_compatible model route {route_name!r} requires a non-empty "
                "base_url in the database (table column); JSON settings cannot substitute)"
            )

    if provider_kind == "anthropic":
        block: dict[str, Any] = dict(rest)
        block["api_key"] = _resolve_secret(route.api_key)
        block["base_url"] = _maybe_str(route.base_url)
        anthropic = _anthropic_settings_from_flat_mapping(block)
        openai_compatible = _inactive_openai_compatible_settings()
        lmstudio = _inactive_lmstudio_settings()
    elif provider_kind == "openai_compatible":
        block_oai = dict(rest)
        provider_max_tokens: int | None = None
        if "max_tokens" in block_oai:
            raw_mt = block_oai.pop("max_tokens", None)
            if raw_mt is not None:
                provider_max_tokens = int(raw_mt)
                if provider_max_tokens < 1:
                    raise ValueError(
                        f"merged patch max_tokens for openai_compatible profile must be >= 1 "
                        f"when set, got {provider_max_tokens}"
                    )
        block_oai["api_key"] = _resolve_secret(route.api_key)
        block_oai["base_url"] = _maybe_str(route.base_url)
        anthropic = _inactive_anthropic_settings()
        openai_compatible = _openai_compatible_settings_from_flat_mapping(
            block_oai, provider_max_tokens=provider_max_tokens
        )
        lmstudio = _inactive_lmstudio_settings()
    else:
        block_ls = dict(rest)
        provider_max_tokens_ls: int | None = None
        if "max_tokens" in block_ls:
            raw_mt_ls = block_ls.pop("max_tokens", None)
            if raw_mt_ls is not None:
                provider_max_tokens_ls = int(raw_mt_ls)
                if provider_max_tokens_ls < 1:
                    raise ValueError(
                        f"merged patch max_tokens for lmstudio profile must be >= 1 "
                        f"when set, got {provider_max_tokens_ls}"
                    )
        block_ls["api_key"] = _resolve_secret(route.api_key)
        block_ls["base_url"] = _maybe_str(route.base_url)
        anthropic = _inactive_anthropic_settings()
        openai_compatible = _inactive_openai_compatible_settings()
        lmstudio = _lmstudio_settings_from_flat_mapping(
            block_ls, provider_max_tokens=provider_max_tokens_ls
        )

    resolved_model = (route.target_model or "").strip()
    if not resolved_model:
        raise ValueError(
            f"model route {route_name!r}: resolved model id is empty; set target_model "
            "to a non-empty string"
        )

    return replace(
        baseline,
        provider=route.route_name,
        provider_kind=provider_kind,
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        signal_strategy=signal_strategy,
        anthropic=anthropic,
        openai_compatible=openai_compatible,
        lmstudio=lmstudio,
    )
