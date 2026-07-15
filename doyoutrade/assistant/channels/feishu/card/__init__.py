"""Feishu Interactive Card support.

IMPORTANT: When adding or modifying Feishu card components, schemas, or element types,
always refer to the official Feishu CardKit documentation:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview

Key resources:
- Card JSON 2.0 schema: https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview
- Available element tags: https://open.feishu.cn/document/feishu-cards/card-json-v2-components
- Card config options: https://open.feishu.cn/document/feishu-cards/card-json-v2-config
- Streaming mode: https://open.feishu.cn/document/feishu-cards/card-json-v2-streaming

Exports:
    CardKitClient
    StreamingCardController
    build_streaming_card
    build_complete_card
    build_confirm_card
    build_approval_resolved_card
    build_ask_user_card
    build_thinking_card
    build_tool_call_card
    ConfirmData
    STREAMING_ELEMENT_ID
"""
from .cardkit import CardKitClient
from .streaming import StreamingCardController
from .builder import (
    build_streaming_card,
    build_complete_card,
    build_confirm_card,
    build_approval_resolved_card,
    build_ask_user_card,
    build_thinking_card,
    build_tool_call_card,
    build_trade_approval_card,
    build_trade_approval_resolved_card,
    build_trade_approval_result_card,
    build_signal_digest_card,
    ConfirmData,
    STREAMING_ELEMENT_ID,
)

__all__ = [
    "CardKitClient",
    "StreamingCardController",
    "build_streaming_card",
    "build_complete_card",
    "build_confirm_card",
    "build_approval_resolved_card",
    "build_ask_user_card",
    "build_thinking_card",
    "build_tool_call_card",
    "build_trade_approval_card",
    "build_trade_approval_resolved_card",
    "build_trade_approval_result_card",
    "build_signal_digest_card",
    "ConfirmData",
    "STREAMING_ELEMENT_ID",
]
