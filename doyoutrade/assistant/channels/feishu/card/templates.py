# doyoutrade/assistant/channels/feishu/card/templates.py
"""Feishu CardKit 模板 JSON — 启动时预创建卡片使用，运行时发送也复用。

IMPORTANT: When modifying card templates, always refer to the official Feishu
CardKit JSON schema documentation:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview

The STREAMING_ELEMENT_ID must match an element_id in the card body to enable
real-time streaming updates via CardKit's element content API.

Template JSON structure follows CardKit 2.0 schema:
{
  "schema": "2.0",
  "config": { wide_screen_mode, streaming_mode, update_multi },
  "body": { "elements": [...] }
}
"""
from __future__ import annotations

from typing import Any

STREAMING_ELEMENT_ID = "streaming_text"

THINKING_CARD_JSON: dict[str, Any] = {
    "schema": "2.0",
    "config": {
        "wide_screen_mode": True,
        "streaming_mode": True,
        "update_multi": True,
    },
    "header": {
        "title": {
            "tag": "plain_text",
            "content": "深度思考",
        },
        "template": "blue",
    },
    "body": {
        "elements": [
            {
                "tag": "markdown",
                "content": "思考中... / Thinking...",
                "element_id": STREAMING_ELEMENT_ID,
            }
        ],
    },
}

STREAMING_CARD_JSON: dict[str, Any] = {
    "schema": "2.0",
    "config": {
        "wide_screen_mode": True,
        "streaming_mode": True,
        "update_multi": True,
    },
    "body": {
        "elements": [
            {
                "tag": "markdown",
                "content": "等待回复... / Waiting...",
                "element_id": STREAMING_ELEMENT_ID,
            }
        ],
    },
}

CONTENT_TYPE_TEMPLATES: dict[str, dict[str, Any]] = {
    "thinking": THINKING_CARD_JSON,
    "tool_call": STREAMING_CARD_JSON,
    "rich_text": STREAMING_CARD_JSON,
}


def get_template_for_content_type(content_type: str) -> dict[str, Any]:
    """根据内容类型返回对应的卡片模板 JSON。"""
    return CONTENT_TYPE_TEMPLATES.get(content_type, STREAMING_CARD_JSON)