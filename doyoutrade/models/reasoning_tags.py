"""Inline ``<think>``-style reasoning tag handling.

Some OpenAI-compatible providers (e.g. MiniMax) do not surface chain-of-thought
via a dedicated ``reasoning_content`` / ``thinking`` delta field the way
DeepSeek does. Instead they inline it into the normal ``content`` stream as
``<think>...</think>`` (or ``<thinking>``/``<thought>``/``<reasoning>``)
markup. Left unhandled, that markup ends up verbatim in the assistant's
visible message text, ``content_blocks``, and session exports.

This module recognizes those tags and separates "text" from "thinking" in
two shapes:

- :class:`ReasoningTagStreamPartitioner` — stateful, chunk-by-chunk splitting
  for streaming deltas (tags may be split across chunk boundaries).
- :func:`strip_reasoning_tags` — one-shot splitting of a complete string, for
  non-streaming responses and defensive re-rendering of already-persisted
  text that may still carry inline tags.
"""

from __future__ import annotations

import re

_REASONING_TAG_RE = re.compile(
    r"<\s*(/?)\s*(?:think(?:ing)?|thought|reasoning)\b[^<>]*>",
    re.IGNORECASE,
)
_REASONING_TAG_NAMES = ("think", "thinking", "thought", "reasoning")


def _tag_body_name(text: str) -> tuple[str, bool]:
    """Return ``(name, is_close)`` for a ``<``-prefixed fragment, name lowercased."""
    body = text[1:]
    is_close = body.startswith("/")
    if is_close:
        body = body[1:]
    return body.lstrip().lower(), is_close


def _is_partial_tag_prefix(fragment: str) -> bool:
    """Whether ``fragment`` (starting with ``<``, no ``>`` yet) could still grow
    into a recognized reasoning tag once more characters arrive."""
    if ">" in fragment:
        return False
    name, _is_close = _tag_body_name(fragment)
    return any(tag.startswith(name) or name.startswith(tag) for tag in _REASONING_TAG_NAMES)


def _find_partial_prefix_index(buffer: str) -> int:
    """Index of the rightmost unresolved ``<...`` that might still complete
    into a reasoning tag, or ``-1`` if the buffer holds no such pending prefix."""
    idx = buffer.rfind("<")
    while idx != -1:
        if _is_partial_tag_prefix(buffer[idx:]):
            return idx
        if idx == 0:
            break
        idx = buffer.rfind("<", 0, idx)
    return -1


class ReasoningTagStreamPartitioner:
    """Splits a stream of text deltas around inline reasoning tags.

    ``push()`` feeds one delta at a time and returns the ``(kind, text)``
    increments that are now safe to emit (``kind`` is ``"text"`` or
    ``"thinking"``). Content that might still be a partial tag (e.g. a chunk
    boundary lands mid ``<think>``) is buffered until the next push resolves
    it. Call ``flush()`` once the stream ends to release anything still
    buffered (an unresolved dangling ``<...`` is emitted as literal text).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_reasoning = False

    def push(self, chunk: str) -> list[tuple[str, str]]:
        if not chunk:
            return []
        self._buffer += chunk
        return self._consume(final=False)

    def flush(self) -> list[tuple[str, str]]:
        return self._consume(final=True)

    def _consume(self, *, final: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []

        def emit(kind: str, text: str) -> None:
            if not text:
                return
            if out and out[-1][0] == kind:
                out[-1] = (kind, out[-1][1] + text)
            else:
                out.append((kind, text))

        while self._buffer:
            mode = "thinking" if self._in_reasoning else "text"
            match = _REASONING_TAG_RE.search(self._buffer)
            if match is None:
                idx = _find_partial_prefix_index(self._buffer)
                if idx == -1:
                    emit(mode, self._buffer)
                    self._buffer = ""
                    break
                if idx > 0:
                    emit(mode, self._buffer[:idx])
                    self._buffer = self._buffer[idx:]
                if final:
                    emit(mode, self._buffer)
                    self._buffer = ""
                break

            before = self._buffer[: match.start()]
            emit(mode, before)
            self._in_reasoning = match.group(1) != "/"
            self._buffer = self._buffer[match.end() :]

        return out


def strip_reasoning_tags(text: str) -> tuple[str, str]:
    """One-shot split of a complete string into ``(visible_text, thinking_text)``.

    For non-streaming responses and for defensively re-rendering
    already-persisted text that may still carry inline ``<think>`` markup.
    """
    partitioner = ReasoningTagStreamPartitioner()
    parts = partitioner.push(text) + partitioner.flush()
    visible = "".join(t for kind, t in parts if kind == "text")
    thinking = "".join(t for kind, t in parts if kind == "thinking")
    return visible, thinking
