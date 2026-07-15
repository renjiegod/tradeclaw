from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # Prefer real LangChain message classes when installed.
    from langchain_core.messages import SystemMessage
except Exception:  # pragma: no cover - dependency fallback for stripped test envs
    from doyoutrade.test_messages import SystemMessage

from doyoutrade.assistant.context_compaction.estimation import estimate_messages_tokens
from doyoutrade.assistant.context_compaction.micro import micro_compact_messages
from doyoutrade.assistant.context_compaction.types import (
    ContextCompactionConfig,
    normalize_context_compaction_config,
)


@dataclass(frozen=True)
class PreparedContextResult:
    messages: list[Any]
    estimated_tokens: int
    micro_compaction_applied: bool
    full_compaction_applied: bool = False


@dataclass(frozen=True)
class FullCompactionDecision:
    should_compact: bool
    threshold_tokens: int


def prepare_messages_for_model(
    *,
    system_prompt: str,
    history_messages: list[Any],
    config: ContextCompactionConfig | dict[str, Any] | None,
) -> PreparedContextResult:
    normalized = normalize_context_compaction_config(config)
    prepared_history = list(history_messages)
    micro_compaction_applied = False

    if normalized["enabled"] and normalized["micro_compaction_enabled"]:
        prepared_history = micro_compact_messages(
            prepared_history,
            tool_result_max_chars=int(normalized["tool_result_max_chars"]),
        )
        micro_compaction_applied = True

    messages = [SystemMessage(content=system_prompt), *prepared_history]
    return PreparedContextResult(
        messages=messages,
        estimated_tokens=estimate_messages_tokens(messages),
        micro_compaction_applied=micro_compaction_applied,
    )


def evaluate_full_compaction(
    *,
    estimated_tokens: int,
    config: ContextCompactionConfig | dict[str, Any] | None,
) -> FullCompactionDecision:
    normalized = normalize_context_compaction_config(config)
    threshold_tokens = max(0, int(normalized["auto_threshold_tokens"]))
    should_compact = (
        bool(normalized["enabled"])
        and str(normalized["mode"]) == "auto"
        and bool(normalized["full_compaction_enabled"])
        and threshold_tokens > 0
        and estimated_tokens >= threshold_tokens
    )
    return FullCompactionDecision(
        should_compact=should_compact,
        threshold_tokens=threshold_tokens,
    )
