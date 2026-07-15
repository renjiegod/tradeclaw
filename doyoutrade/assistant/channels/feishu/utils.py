"""Feishu message parsing utilities."""
from __future__ import annotations

import json
import re
from typing import Any

from doyoutrade.assistant.channels.base import (
    AudioContent,
    ContentPart,
    FileContent,
    ImageContent,
    TextContent,
)

# Collapse the whitespace gaps left behind once a mention placeholder is removed.
_WS_RUN = re.compile(r"[ \t ]{2,}")


def clean_feishu_text(
    text: str,
    mentions: list[dict[str, Any]] | None,
    bot_open_id: str = "",
) -> str:
    """Resolve Feishu @-mention placeholders (``@_user_N`` / ``@_all``) in text.

    In group chats Feishu does not inline the @-name; it injects a placeholder
    token into ``content.text`` and ships the real identities in the message's
    ``mentions`` array. Each mention dict here is expected to be normalized to
    ``{"key": "@_user_1", "open_id": "ou_...", "name": "Tom"}``.

    Rules (see CLAUDE.md — failures must stay visible, no silent fallbacks):
    - The bot's own @ is pure addressing metadata, not content → stripped so a
      bare command like ``/new`` survives ``parse_lifecycle_command``.
    - Other users' placeholders are replaced with a readable ``@name`` so the
      remaining instruction keeps its meaning.
    - When ``bot_open_id`` could not be resolved we cannot tell which mention is
      the bot, so every placeholder is stripped. The caller logs a warning in
      that path; this keeps commands working rather than silently breaking them.
    """
    if not text or not mentions:
        return text

    cleaned = text
    for mention in mentions:
        key = (mention.get("key") or "") if isinstance(mention, dict) else ""
        if not key:
            continue
        open_id = (mention.get("open_id") or "") if isinstance(mention, dict) else ""
        name = (mention.get("name") or "") if isinstance(mention, dict) else ""
        if not bot_open_id:
            replacement = ""  # cannot identify the bot → strip all (logged upstream)
        elif open_id == bot_open_id:
            replacement = ""  # bot's own @ is addressing, not content
        else:
            replacement = f"@{name}" if name else ""
        cleaned = cleaned.replace(key, replacement)

    cleaned = _WS_RUN.sub(" ", cleaned).strip()
    return cleaned


def parse_feishu_message_content(
    msg_type: str,
    content_json: str,
    mentions: list[dict[str, Any]] | None = None,
    bot_open_id: str = "",
) -> list[ContentPart]:
    """解析飞书消息 content JSON，返回 ContentPart 列表。

    Args:
        msg_type: 飞书消息类型，如 "text", "image", "file", "audio"
        content_json: 飞书消息的 content 字段（JSON 字符串）
        mentions: 规整后的 @ 列表（``key`` / ``open_id`` / ``name``），群聊里
            飞书会把 @ 替换成 ``@_user_N`` 占位符并单独下发这个数组。
        bot_open_id: 机器人自己的 open_id，用于只剥机器人的 @。

    Returns:
        ContentPart 列表
    """
    try:
        data = json.loads(content_json) if isinstance(content_json, str) else content_json
    except Exception:
        return [TextContent(text=str(content_json))]

    if msg_type == "text":
        text = clean_feishu_text(data.get("text", ""), mentions, bot_open_id)
        return [TextContent(text=text)]
    elif msg_type == "image":
        return [ImageContent(image_id=data.get("image_key"))]
    elif msg_type == "file":
        return [FileContent(file_id=data.get("file_key"), name=data.get("file_name"))]
    elif msg_type == "audio":
        return [AudioContent(audio_id=data.get("audio_key"))]
    else:
        # post, media 等未知类型，尝试返回文本表示
        return [TextContent(text=content_json)]
