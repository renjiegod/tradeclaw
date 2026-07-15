from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ContextCompactionConfig:
    enabled: bool = True
    mode: str = "auto"
    trigger_strategy: str = "token_estimate"
    auto_threshold_tokens: int = 24000
    warning_threshold_tokens: int = 20000
    preserve_recent_messages: int = 12
    preserve_recent_tool_pairs: int = 4
    micro_compaction_enabled: bool = True
    tool_result_max_chars: int = 4000
    full_compaction_enabled: bool = True
    summary_model_route_name: str = ""
    allow_slash_compact: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONTEXT_COMPACTION: dict[str, Any] = ContextCompactionConfig().as_dict()


def normalize_context_compaction_config(
    value: Mapping[str, Any] | ContextCompactionConfig | None,
) -> dict[str, Any]:
    normalized = dict(DEFAULT_CONTEXT_COMPACTION)
    if value is None:
        return normalized
    if isinstance(value, ContextCompactionConfig):
        normalized.update(value.as_dict())
        return normalized
    normalized.update({key: item for key, item in dict(value).items() if item is not None})
    return normalized
