from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from doyoutrade.money.decimal_helpers import json_default_with_decimals

_LOG = logging.getLogger("doyoutrade.models")
_DEBUG_RESPONSE_TEXT_MAX = 8192


def _format_raw_model_output(raw: Any) -> str:
    """Stringify provider-native payload (e.g. LangChain message ``content``)."""
    if raw is None:
        return "<no raw>"
    payload: Any = getattr(raw, "content", raw)
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=json_default_with_decimals)
    except (TypeError, ValueError):
        return repr(payload)


def _truncate_debug_text(s: str, max_len: int) -> tuple[str, int]:
    n = len(s)
    if n <= max_len:
        return s, n
    return s[:max_len] + f"... [truncated, total {n} chars]", n


#: Image MIME types accepted by :class:`ImagePart` (both OpenAI-compatible and
#: Anthropic vision endpoints accept exactly these four).
ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: Hard cap on a single image payload (bytes, pre-base64). Anthropic caps at
#: ~5 MB post-encode; 8 MB raw is a generous shared ceiling for both providers.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ImagePart:
    """One raw image attached to a :class:`ModelRequest` user turn.

    Validation is eager (schema violation → raise with actual size/type, per
    AGENTS.md §错误可见性 — never silently truncate or coerce):

    - ``data`` must be non-empty ``bytes`` and ≤ :data:`MAX_IMAGE_BYTES`.
    - ``mime_type`` must be one of :data:`ALLOWED_IMAGE_MIME_TYPES`.
    """

    data: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)) or len(self.data) == 0:
            raise ValueError(
                "ImagePart.data must be non-empty bytes, got "
                f"{type(self.data).__name__} of length "
                f"{len(self.data) if isinstance(self.data, (bytes, bytearray)) else 'n/a'}"
            )
        if len(self.data) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"ImagePart.data is {len(self.data)} bytes, exceeds the "
                f"{MAX_IMAGE_BYTES} byte limit"
            )
        if self.mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            allowed = ", ".join(sorted(ALLOWED_IMAGE_MIME_TYPES))
            raise ValueError(
                f"ImagePart.mime_type must be one of {allowed}; got {self.mime_type!r}"
            )


@dataclass(frozen=True)
class ModelRequest:
    """Single-turn chat request. Optional *tools* are passed to the provider (``bind_tools`` on LangChain clients).

    ``image_parts`` (optional, default ``None``) attaches images to the user
    turn for vision-capable models. Providers encode them as multimodal
    content blocks; the recording layer replaces the base64 payload with a
    ``<image: N bytes, mime>`` placeholder so raw image data never lands in
    ``model_invocations`` (see ``providers._common.redact_image_blocks``).
    """

    system_prompt: str
    user_prompt: str
    tools: list[dict[str, Any]] | None = None
    image_parts: tuple[ImagePart, ...] | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    raw: Any = None
    #: Exact provider API request kwargs (e.g. ``chat.completions.create``), for persistence.
    invocation_request_payload: dict[str, Any] | None = None
    #: Full serialized provider API response body, for persistence.
    invocation_response_payload: dict[str, Any] | None = None


def log_model_response_debug(response: ModelResponse, *, adapter: str) -> None:
    """Log provider-native model output at DEBUG (``raw.content`` when present)."""
    if not _LOG.isEnabledFor(logging.DEBUG):
        return
    raw_full = _format_raw_model_output(response.raw)
    raw_preview, raw_total = _truncate_debug_text(raw_full, _DEBUG_RESPONSE_TEXT_MAX)
    extracted_n = len(response.text)
    _LOG.debug(
        "model raw output adapter=%s raw_chars=%d extracted_chars=%d raw=%s",
        adapter,
        raw_total,
        extracted_n,
        raw_preview,
    )


class ModelAdapter(ABC):
    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a model response from the provided prompts."""
