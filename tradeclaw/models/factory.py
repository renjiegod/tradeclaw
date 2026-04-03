from __future__ import annotations

from tradeclaw.config import ModelSettings
from tradeclaw.models.providers import AnthropicAdapter, OpenAICompatibleAdapter


def build_model_adapter(settings: ModelSettings):
    provider = settings.provider.strip().lower()

    if provider == "anthropic":
        if not settings.anthropic.api_key:
            raise ValueError("model.anthropic.api_key is required for anthropic provider")
        return AnthropicAdapter(
            model=settings.model,
            api_key=settings.anthropic.api_key,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            timeout_seconds=settings.timeout_seconds,
            base_url=settings.anthropic.base_url,
        )

    if provider == "openai_compatible":
        if not settings.openai_compatible.api_key:
            raise ValueError("model.openai_compatible.api_key is required for openai_compatible provider")
        if not settings.openai_compatible.base_url:
            raise ValueError("model.openai_compatible.base_url is required for openai_compatible provider")
        return OpenAICompatibleAdapter(
            model=settings.model,
            api_key=settings.openai_compatible.api_key,
            base_url=settings.openai_compatible.base_url,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            timeout_seconds=settings.timeout_seconds,
        )

    raise ValueError(f"unsupported model provider: {settings.provider}")
