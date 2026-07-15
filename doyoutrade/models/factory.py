from __future__ import annotations

from doyoutrade.config import ModelSettings
from doyoutrade.models.base import ModelAdapter
from doyoutrade.models.providers import AnthropicAdapter, OpenAICompatibleAdapter
from doyoutrade.models.providers.lmstudio import LmStudioAdapter
from doyoutrade.models.recording import ModelInvocationRecorder, RecordingModelAdapter


def build_model_adapter(settings: ModelSettings):
    kind = settings.provider_kind.strip().lower()
    profile = settings.provider.strip()

    if kind == "anthropic":
        if not settings.anthropic.api_key:
            raise ValueError(
                f"providers[{profile!r}] (anthropic) requires a non-null api_key"
            )
        anthropic_max_tokens = settings.max_tokens if settings.max_tokens is not None else 100000
        return AnthropicAdapter(
            model=settings.model,
            api_key=settings.anthropic.api_key,
            temperature=settings.temperature,
            max_tokens=anthropic_max_tokens,
            timeout_seconds=settings.timeout_seconds,
            base_url=settings.anthropic.base_url,
            thinking=settings.anthropic.thinking,
            cache_control=settings.anthropic.cache_control,
        )

    if kind == "openai_compatible":
        if not settings.openai_compatible.api_key:
            raise ValueError(
                f"providers[{profile!r}] (openai_compatible) requires a non-null api_key"
            )
        if not settings.openai_compatible.base_url:
            raise ValueError(
                f"providers[{profile!r}] (openai_compatible) requires a non-null base_url"
            )
        effective_max_tokens = (
            settings.openai_compatible.max_tokens
            if settings.openai_compatible.max_tokens is not None
            else settings.max_tokens
        )
        return OpenAICompatibleAdapter(
            model=settings.model,
            api_key=settings.openai_compatible.api_key,
            base_url=settings.openai_compatible.base_url,
            temperature=settings.temperature,
            max_tokens=effective_max_tokens,
            timeout_seconds=settings.timeout_seconds,
            tool_choice=settings.openai_compatible.tool_choice,
        )

    if kind == "lmstudio":
        effective_max_tokens = (
            settings.lmstudio.max_tokens
            if settings.lmstudio.max_tokens is not None
            else settings.max_tokens
        )
        return LmStudioAdapter(
            settings.model,
            api_key=settings.lmstudio.api_key,
            base_url=settings.lmstudio.base_url,
            temperature=settings.temperature,
            max_tokens=effective_max_tokens,
            timeout_seconds=settings.timeout_seconds,
            tool_choice=settings.lmstudio.tool_choice,
            prediction_config_extra=settings.lmstudio.prediction_config_extra,
        )

    raise ValueError(f"unsupported model.provider_kind: {settings.provider_kind!r}")


def wrap_with_recording(
    adapter: ModelAdapter,
    *,
    provider: str,
    provider_kind: str,
    model: str,
    recorder: ModelInvocationRecorder | None,
) -> ModelAdapter:
    if recorder is None:
        return adapter
    return RecordingModelAdapter(
        inner=adapter,
        provider=provider.strip().lower(),
        provider_kind=provider_kind.strip().lower(),
        model=model,
        recorder=recorder,
    )
