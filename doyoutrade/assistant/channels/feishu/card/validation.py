"""Validation helpers for Feishu Card JSON 2.0 builders.

These checks intentionally cover the local contract that has broken in real
Feishu clients before: legacy V1 containers, unsupported tags, and button rows
that render as ellipses on mobile. They are used by unit tests and by the
manual smoke script so the two paths do not drift.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


UNSUPPORTED_V2_TAGS = {"action", "note"}
MAX_BUTTONS_PER_ROW = 2


def iter_tagged_nodes(value: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any], str]]:
    """Yield every dict with a ``tag`` field plus its JSON-ish path."""
    if isinstance(value, dict):
        tag = value.get("tag")
        if isinstance(tag, str):
            yield tag, value, path
        for key, child in value.items():
            yield from iter_tagged_nodes(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_tagged_nodes(child, f"{path}[{index}]")


def _count_buttons_in_elements(elements: Any) -> int:
    if not isinstance(elements, list):
        return 0
    count = 0
    for item in elements:
        if not isinstance(item, dict):
            continue
        if item.get("tag") == "button":
            count += 1
        elif item.get("tag") == "column_set":
            count += _count_buttons_in_column_set(item)
        else:
            count += _count_buttons_in_elements(item.get("elements"))
    return count


def _count_buttons_in_column_set(node: dict[str, Any]) -> int:
    count = 0
    for column in node.get("columns") or []:
        if isinstance(column, dict):
            count += _count_buttons_in_elements(column.get("elements"))
    return count


def _has_button_callback_value(node: dict[str, Any]) -> bool:
    if isinstance(node.get("value"), dict):
        return True
    behaviors = node.get("behaviors")
    if not isinstance(behaviors, list):
        return False
    for behavior in behaviors:
        if (
            isinstance(behavior, dict)
            and behavior.get("type") == "callback"
            and isinstance(behavior.get("value"), dict)
        ):
            return True
    return False


def validate_card_json_v2(card: dict[str, Any], *, name: str = "card") -> list[str]:
    """Return contract errors for a Feishu Card JSON 2.0 card."""
    errors: list[str] = []
    if not isinstance(card, dict):
        return [f"{name}: card must be a dict"]
    if card.get("schema") != "2.0":
        errors.append(f"{name}: schema must be '2.0'")
    body = card.get("body")
    if not isinstance(body, dict):
        errors.append(f"{name}: body must be a dict")
    elif not isinstance(body.get("elements"), list):
        errors.append(f"{name}: body.elements must be a list")

    for tag, node, path in iter_tagged_nodes(card):
        if tag in UNSUPPORTED_V2_TAGS:
            errors.append(f"{name}: unsupported Card JSON 2.0 tag {tag!r} at {path}")
        if tag == "column_set":
            columns = node.get("columns")
            if not isinstance(columns, list) or not columns:
                errors.append(f"{name}: column_set at {path} must have non-empty columns")
                continue
            button_count = _count_buttons_in_column_set(node)
            if button_count > MAX_BUTTONS_PER_ROW:
                errors.append(
                    f"{name}: column_set at {path} has {button_count} buttons; "
                    f"max {MAX_BUTTONS_PER_ROW} to avoid mobile ellipsis"
                )
        if tag == "button":
            text = node.get("text")
            content = text.get("content") if isinstance(text, dict) else None
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{name}: button at {path} must have non-empty text.content")
            if not _has_button_callback_value(node):
                errors.append(
                    f"{name}: button at {path} must carry a dict value "
                    "or behaviors.callback.value"
                )
    return errors


def assert_valid_card_json_v2(card: dict[str, Any], *, name: str = "card") -> None:
    """Raise ``AssertionError`` if ``card`` violates the local V2 contract."""
    errors = validate_card_json_v2(card, name=name)
    if errors:
        raise AssertionError("\n".join(errors))


def sample_feishu_cards() -> dict[str, dict[str, Any]]:
    """Build representative cards for contract tests and manual smoke sends."""
    from .builder import (
        ConfirmData,
        build_approval_card,
        build_approval_resolved_card,
        build_ask_user_card,
        build_complete_card,
        build_confirm_card,
        build_signal_digest_card,
        build_streaming_card,
        build_thinking_card,
        build_tool_call_card,
        build_trade_approval_card,
        build_trade_approval_resolved_card,
        build_trade_approval_result_card,
    )

    approval_payload = {
        "approval_id": "appr-smoke",
        "description": "停止交易任务",
        "command_preview": "doyoutrade-cli task stop task-smoke",
        "timeout_seconds": 300,
    }
    trade_payload = {
        "approval_id": "tappr-smoke",
        "intent_id": "intent-smoke",
        "task_id": "task-smoke",
        "symbol": "600000.SH",
        "symbol_name": "浦发银行",
        "action": "buy",
        "notional": "10000.00",
        "strategy_tag": "grid",
        "created_at": "2026-06-18T09:30:00+08:00",
        "timeout_seconds": 300,
    }
    digest = {
        "status": "ok",
        "run_mode": "signal_only",
        "details": {
            "market_snapshot": {
                "600000.SH": {
                    "last_price": "10.00",
                    "pct_change": "1.20",
                    "turnover": "12345678",
                }
            },
            "signal_diagnostics": {
                "600000.SH": {"signal": "buy", "rationale": "测试信号"}
            },
            "position_intents": [
                {"symbol": "600000.SH", "action": "buy", "amount": "10000"}
            ],
        },
    }
    return {
        "streaming": build_streaming_card("测试回复"),
        "complete": build_complete_card("测试完成", elapsed_ms=123),
        "thinking": build_thinking_card("正在思考"),
        "tool_call": build_tool_call_card(
            {
                "id": "call-smoke",
                "name": "execute_bash",
                "category": "tool",
                "input": {"cmd": "doyoutrade-cli task stop task-smoke"},
                "status": "completed",
                "result": {"output": {"ok": True}, "is_error": False},
            }
        ),
        "confirm_three_buttons": build_confirm_card(
            ConfirmData(operation_description="确认写入？", pending_operation_id="op-smoke")
        ),
        "approval_pending": build_approval_card(approval_payload),
        "approval_approved": build_approval_resolved_card(
            approval_payload, decision="approve_once", resolver="ou_smoke"
        ),
        "approval_rejected": build_approval_resolved_card(
            approval_payload, decision="reject", resolver="ou_smoke"
        ),
        "ask_user_three_options": build_ask_user_card(
            {
                "question_id": "uq-smoke",
                "question": "请选择一个测试选项",
                "header": "测试问题",
                "options": [
                    {"label": "选项 A", "description": "第一项"},
                    {"label": "选项 B", "description": "第二项"},
                    {"label": "选项 C", "description": "第三项"},
                ],
            }
        ),
        "trade_approval_pending": build_trade_approval_card(trade_payload),
        "trade_approval_approved": build_trade_approval_resolved_card(
            trade_payload, decision="approve", resolver="ou_smoke"
        ),
        "trade_approval_result": build_trade_approval_result_card(
            trade_payload, outcome="filled"
        ),
        "signal_digest": build_signal_digest_card(
            trigger_name="测试触发器",
            digest=digest,
            processed_at="2026-06-18 09:35:00",
            run_id="run-smoke",
            task_id="task-smoke",
            task_name="测试任务",
            symbol_names={"600000.SH": "浦发银行"},
        ),
    }


__all__ = [
    "MAX_BUTTONS_PER_ROW",
    "UNSUPPORTED_V2_TAGS",
    "assert_valid_card_json_v2",
    "iter_tagged_nodes",
    "sample_feishu_cards",
    "validate_card_json_v2",
]
