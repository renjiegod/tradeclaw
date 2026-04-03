from __future__ import annotations

from typing import Any, Iterable

from tradeclaw.models.base import ModelAdapter, ModelRequest, ModelResponse


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        model: str,
        api_key: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        base_url: str | None = None,
    ):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("langchain-anthropic is not installed") from exc

        self.client = ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
            base_url=base_url,
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        messages = _build_messages(request)
        result = self.client.invoke(messages)
        return ModelResponse(text=_extract_text(result.content), raw=result)


class OpenAICompatibleAdapter(ModelAdapter):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
    ):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("langchain-openai is not installed") from exc

        self.client = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        messages = _build_messages(request)
        result = self.client.invoke(messages)
        return ModelResponse(text=_extract_text(result.content), raw=result)


def _build_messages(request: ModelRequest):
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("langchain-core is not installed") from exc

    return [
        SystemMessage(content=request.system_prompt),
        HumanMessage(content=request.user_prompt),
    ]


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, Iterable):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                value = item.get("text")
                if value is not None:
                    parts.append(str(value))
        if parts:
            return "\n".join(parts)

    return str(content)
