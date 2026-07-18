"""CardKit 2.0 JSON builder for Feishu interactive cards.

IMPORTANT: When adding or modifying card element structures, always refer to the
official Feishu CardKit component documentation:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview

Supported element tags in this module:
- markdown / lark_md: Rich text rendering
- div: Container element
- hr: Horizontal rule
- button: Interactive button (with value payload)
- column_set / column: Button layout container for Card JSON 2.0
- input: Text input field
- select_static: Static dropdown select
- collapsible_panel: Expandable/collapsible panel

Card schema 2.0 config options:
- wide_screen_mode: Boolean
- streaming_mode: Boolean (enables real-time updates)
- update_multi: Boolean (allows multiple element updates)

For any new element types or card configurations, consult:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .templates import STREAMING_ELEMENT_ID, THINKING_CARD_JSON, STREAMING_CARD_JSON

FEISHU_CARD_TABLE_LIMIT = 50

# Fixed compliance footnote appended to every pushed signal / alert / trade card.
# Rendered here in PURE PYTHON (never routed through an LLM composer) so it cannot
# be reworded, softened, or dropped by a hallucinating model — the non-advice
# framing is a hard, code-level guarantee. This is the display-edge complement to
# the behavioral half in ``main_agent.j2`` 「应答纪律：不荐股 / 不预测 / 不承诺收益」.
RISK_DISCLAIMER_TEXT = (
    "本卡片为规则 / 策略自动生成的客观信息，非投资建议、不构成买卖推荐；"
    "据此操作风险自负，交易决策与下单由你本人负责。"
)


def _risk_disclaimer_elements() -> list[dict[str, Any]]:
    """Code-rendered (non-LLM) 免责 footnote: ``hr`` + a grey markdown line."""
    return [
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>⚠️ {RISK_DISCLAIMER_TEXT}</font>",
            },
        },
    ]


@dataclass
class ConfirmData:
    operation_description: str
    pending_operation_id: str
    preview: str | None = None


def _button_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """Render buttons in a Card JSON 2.0-compatible horizontal row.

    Feishu CardKit V2 rejects the legacy ``{"tag": "action", "elements": ...}``
    container. Keep buttons as normal V2 components and use columns for layout.
    """
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def _button_stack(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render one full-width button per row to avoid Feishu mobile ellipsis."""
    return [_button_row([button]) for button in buttons]


def _button_group(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render buttons with a safe default layout.

    Feishu mobile clients truncate text aggressively when three or more buttons
    share a row, so keep at most two buttons per row.
    """
    if len(buttons) <= 2:
        return [_button_row(buttons)]
    return _button_stack(buttons)


def _stop_button_row(session_id: str) -> dict[str, Any]:
    """A single full-width 停止 button that aborts the in-flight assistant attempt.

    The click is routed by ``channel._on_card_action_trigger`` via
    ``action == "stop_attempt"`` to ``AssistantService.stop_attempt(session_id)``.
    The ``session_id`` is carried in the button value because the card-action event
    payload otherwise has no session reference — the handler cannot otherwise tell
    which running attempt to abort. Only updates ``stream_card_content`` touch the
    markdown element while streaming, so this button persists across deltas and is
    dropped only when the terminal ``build_complete_card`` replaces the whole body.
    """
    return _button_row(
        [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏹ 停止"},
                "type": "danger",
                "value": {"action": "stop_attempt", "session_id": session_id},
            }
        ]
    )


def build_streaming_card(
    partial_text: str,
    show_tool_use: bool = True,
    reasoning_text: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build a streaming card for real-time text updates.

    CardKit 2.0 schema with wide_screen_mode and update_multi enabled. When
    ``session_id`` is given, a 停止 button is appended so the operator can abort the
    running attempt straight from the streaming card (see :func:`_stop_button_row`).
    """
    card = STREAMING_CARD_JSON.copy()
    content = partial_text or "等待回复... / Waiting..."
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": _optimize_markdown(content),
            "element_id": STREAMING_ELEMENT_ID,
        }
    ]
    if session_id:
        elements.append(_stop_button_row(session_id))
    card["body"] = {"elements": elements}
    return card


def build_complete_card(
    text: str,
    show_tool_use: bool = True,
    reasoning_text: str | None = None,
    reasoning_elapsed_ms: int | None = None,
    elapsed_ms: int | None = None,
    is_error: bool = False,
    is_aborted: bool = False,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a complete (final) card with all content rendered."""
    elements: list[dict[str, Any]] = []

    # Main text content as markdown
    elements.append(
        {
            "tag": "markdown",
            "content": _optimize_markdown(text),
            "element_id": STREAMING_ELEMENT_ID,
        }
    )

    # Footer with status and elapsed time. Card JSON 2.0 does not support note.
    footer_parts: list[str] = []
    if is_error:
        footer_parts.append("❌ 执行出错")
    elif is_aborted:
        footer_parts.append("⏹ 已终止")
    else:
        footer_parts.append("✅ 完成")

    if elapsed_ms is not None:
        footer_parts.append(_format_elapsed(elapsed_ms))
    if reasoning_elapsed_ms is not None:
        footer_parts.append(f"💭 {_format_elapsed(reasoning_elapsed_ms)}")

    elements.append(
        {
            "tag": "hr",
        }
    )
    elements.append(
        {
            "tag": "markdown",
            "content": " · ".join(footer_parts),
        }
    )

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "body": {
            "elements": elements,
        },
    }


def build_confirm_card(data: ConfirmData) -> dict[str, Any]:
    """Build a confirmation card with Confirm/Reject buttons."""
    header = {
        "title": {
            "tag": "plain_text",
            "content": "🔒 Confirmation Required",
        },
    }

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": data.operation_description,
            },
        }
    ]

    if data.preview:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Preview:**\n{data.preview}",
                },
            }
        )

    # Action buttons
    button_elements: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ Confirm"},
            "type": "primary",
            "value": {"action": "confirm_write", "operation_id": data.pending_operation_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "❌ Reject"},
            "type": "default",
            "value": {"action": "reject_write", "operation_id": data.pending_operation_id},
        },
    ]

    if not data.preview:
        button_elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "👁 Preview"},
                "type": "default",
                "value": {"action": "preview_write", "operation_id": data.pending_operation_id},
            }
        )

    elements.extend(_button_group(button_elements))

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": "orange",
            "title": header["title"],
        },
        "body": {
            "elements": elements,
        },
    }


def build_approval_card(payload: dict[str, Any]) -> dict[str, Any]:
    """Interactive card for a pending tool-call approval.

    ``payload`` is ``ApprovalRequest.payload()``. Buttons carry
    ``action="approval_resolve"`` + the decision; the channel resolves the
    broker future directly (no synthetic message round-trip).

    Four choices when ``allow_always`` (ClaudeCode-style): once / session /
    persist-with-editable-prefix / reject-with-reason. Form inputs carry the
    editable command prefix and optional reject reason.
    """
    approval_id = str(payload.get("approval_id") or "")
    description = str(payload.get("description") or "高危操作")
    command_preview = str(payload.get("command_preview") or "")
    timeout_seconds = int(payload.get("timeout_seconds") or 0)
    suggested_prefix = str(payload.get("suggested_prefix") or "").strip()
    allow_always = bool(payload.get("allow_always", True))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"Agent 请求执行以下操作，需要你的审批：\n**{description}**",
            },
        }
    ]
    if command_preview:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"```\n{command_preview}\n```"},
            }
        )
    if timeout_seconds:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"<font color='grey'>{timeout_seconds} 秒内未响应将自动取消执行。</font>"
                    ),
                },
            }
        )

    if allow_always:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**命令前缀**（「本会话总是允许 / 写入 settings」可改；"
                        "留空则按规则记住）"
                    ),
                },
            }
        )
        elements.append(
            {
                "tag": "input",
                "name": "approval_command_prefix",
                "element_id": f"approval_prefix_{approval_id}",
                "default_value": suggested_prefix,
                "placeholder": {
                    "tag": "plain_text",
                    "content": suggested_prefix or "例如 doyoutrade-cli task start:*",
                },
            }
        )

    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**拒绝原因**（点「拒绝」时可选填写）",
            },
        }
    )
    elements.append(
        {
            "tag": "input",
            "name": "approval_reject_reason",
            "element_id": f"approval_reason_{approval_id}",
            "placeholder": {
                "tag": "plain_text",
                "content": "说明为什么拒绝，便于 Agent 调整方案…",
            },
        }
    )

    def _button(label: str, decision: str, btn_type: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btn_type,
            "value": {
                "action": "approval_resolve",
                "approval_id": approval_id,
                "decision": decision,
                "description": description,
                "command_preview": command_preview,
                "suggested_prefix": suggested_prefix,
            },
        }

    buttons = [_button("允许一次", "approve_once", "primary")]
    if allow_always:
        buttons.append(_button("本会话总是允许", "approve_always", "default"))
        buttons.append(_button("写入 settings", "approve_persist", "default"))
    buttons.append(_button("拒绝", "reject", "danger"))
    elements.extend(_button_stack(buttons))

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "🔒 操作需要审批"},
        },
        "body": {"elements": elements},
    }


def build_approval_resolved_card(
    payload: dict[str, Any],
    *,
    decision: str,
    resolver: str = "",
    reason: str = "",
    command_prefix: str = "",
) -> dict[str, Any]:
    """Terminal card for a resolved assistant tool-call approval."""
    decision_norm = str(decision or "").strip().lower()
    approved = decision_norm in (
        "approve_once",
        "approve_always",
        "approve_persist",
        "approve",
        "approved",
    )
    description = str(payload.get("description") or "高危操作")
    command_preview = str(payload.get("command_preview") or "")

    if approved:
        template = "green"
        title = "✅ 操作审批 · 已批准"
        status_line = "审批已通过，Agent 将继续执行该操作。"
        if decision_norm == "approve_always":
            status_line = "已批准，并在本会话记住该授权。"
        elif decision_norm == "approve_persist":
            status_line = "已批准，并写入 settings 持久记住。"
        if command_prefix:
            status_line += f"\n前缀：`{command_prefix}`"
    else:
        template = "grey"
        title = "🚫 操作审批 · 已拒绝"
        status_line = "审批已拒绝，该操作不会执行。"
        if reason:
            status_line += f"\n原因：{reason}"

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": status_line}},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**操作**\n{description}"},
        },
    ]
    if command_preview:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"```\n{command_preview}\n```"},
            }
        )
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>决议人：{resolver or '—'}</font>",
            },
        }
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def _trade_action_label(action: str) -> str:
    """Map the order side (``buy`` / ``sell``) to a Chinese label."""
    normalized = str(action or "").strip().lower()
    if normalized == "buy":
        return "买入"
    if normalized == "sell":
        return "卖出"
    # Unknown side must stay visible rather than silently render an empty cell.
    return action or "未知方向"


_ORDER_TYPE_LABELS = {"limit": "限价单", "market": "市价单"}
_TIF_LABELS = {
    "day": "当日有效",
    "gtc": "撤销前有效",
    "ioc": "即时成交否则取消",
    "fok": "全额成交否则取消",
}


def _order_type_label(order_type: str) -> str:
    return _ORDER_TYPE_LABELS.get(str(order_type or "").strip().lower(), order_type or "—")


def _tif_label(tif: str) -> str:
    return _TIF_LABELS.get(str(tif or "").strip().lower(), tif or "—")


# Agent narration can hallucinate; it is ALWAYS shown under this advisory caption,
# kept SEPARATE from the deterministic 下单信息 facts (CLAUDE.md §错误可见性 — a wrong
# number must read as opinion, never as the order). Shared verbatim by the pending +
# 终态 cards so the separation is byte-identical wherever a narration is shown.
_AI_INTERPRETATION_CAPTION = (
    "**🤖 AI 解读**（仅供参考，**下单不依据此文本**，请以下方「下单信息」为准）"
)
# The Agent narration is carried in the button callback value (display-only) so the
# terminal card can re-render it after a click — the click event carries no card body,
# only the button value (see channel._resolve_trade_approval._detail_payload). Capped
# so the callback value stays small (the operator already saw the full text on the
# pending card). NEVER routed into execution: the order is keyed by approval_id alone.
_APPROVAL_CALLBACK_NARRATION_MAX = 800


def _ai_interpretation_elements(narration: str | None) -> list[dict[str, Any]]:
    """AI 解读 block (caption + body + hr), or [] when there is no narration.

    Empty narration → empty list so card/none mode renders no AI section at all.
    """
    text = str(narration or "").strip()
    if not text:
        return []
    return [
        {"tag": "div", "text": {"tag": "lark_md", "content": _AI_INTERPRETATION_CAPTION}},
        {"tag": "div", "text": {"tag": "lark_md", "content": _optimize_markdown(text)}},
        {"tag": "hr"},
    ]


def _build_trade_approval_detail_elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the shared 信号 + order-detail body shared by pending + resolved cards.

    Renders the SAME rich 信号 data the pure trading-signal digest carried, scoped
    to this order: 标的 / 方向(买卖 + 信号方向[signal_tag]) / 现价(涨跌幅) / 限价 /
    名义金额 / 订单类型·有效期 / 策略 / (平仓原因) / 任务 / 创建时间 as a two-column
    ``div.fields`` block, plus a full-width 理由. 现价/涨跌幅/信号方向 are the
    signal-time market + 判断 from the order's cycle (empty when unavailable).
    Both the 待审批 and 终态 cards render from this (no drift), and it matches the
    web/Chat ``ApprovalQueueCard``.
    """
    symbol = str(payload.get("symbol") or "—")
    symbol_name = str(payload.get("symbol_name") or "").strip()
    symbol_display = f"{symbol_name}（{symbol}）" if symbol_name else symbol
    action_label = _trade_action_label(str(payload.get("action") or ""))
    # notional / 价格 are decimal STRINGS (§硬约束: 金额十进制) — never to float.
    notional = str(payload.get("notional") or "—")
    price_reference = str(payload.get("price_reference") or "—")
    last_price = str(payload.get("last_price") or "").strip()
    pct_change = str(payload.get("pct_change") or "").strip()
    direction = str(payload.get("direction") or "").strip()
    signal_tag = str(payload.get("signal_tag") or "").strip()
    strategy_tag = str(payload.get("strategy_tag") or "—")
    order_type = _order_type_label(str(payload.get("order_type") or ""))
    tif = _tif_label(str(payload.get("tif") or ""))
    exit_reason = str(payload.get("exit_reason") or "").strip()
    task_id = str(payload.get("task_id") or "—")
    created_at = str(payload.get("created_at") or "—")
    rationale = str(payload.get("rationale") or "").strip()

    quote = f"{last_price} ({pct_change})" if last_price and pct_change else (last_price or "—")
    dir_text = (direction or action_label) + (f" [{signal_tag}]" if signal_tag else "")

    fields = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**标的**\n{symbol_display}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**\n{dir_text}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**现价**\n{quote}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**限价**\n{price_reference}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**名义金额**\n{notional}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**订单**\n{order_type} · {tif}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**策略**\n{strategy_tag}"}},
    ]
    if exit_reason:
        fields.append(
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**平仓原因**\n{exit_reason}"}}
        )
    fields.extend(
        [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n{task_id}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**创建时间**\n{created_at}"}},
        ]
    )
    elements: list[dict[str, Any]] = [{"tag": "div", "fields": fields}]
    if rationale:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**理由**\n{rationale}"},
            }
        )
    return elements


def build_trade_approval_card(
    payload: dict[str, Any], narration: str | None = None
) -> dict[str, Any]:
    """Interactive card for a pending LIVE trade-order approval.

    ``payload`` carries the execution-side approval snapshot fields: ``symbol``,
    ``symbol_name``, ``action`` (``buy``/``sell``), ``notional`` (decimal string),
    ``strategy_tag``, ``task_id``, ``created_at``, ``approval_id``, ``intent_id``
    and optional ``timeout_seconds`` / ``expires_at`` + signal context.

    SAFETY: the deterministic 下单信息 fields block (built from the persisted
    intent + catalog + cycle digest) is ALWAYS rendered and labeled「成交以此为准」.
    The Agent narration — which can hallucinate — is a SEPARATE, clearly captioned
    「🤖 AI 解读」block above it, marked advisory ("下单不依据此文本"). Execution never
    reads the narration (it is transient, never persisted); the operator can
    cross-check the prose against the authoritative facts and spot any drift.
    The approve/reject buttons + their fact-carrying value are IDENTICAL either
    way (功能不阉割).
    - ``narration`` given (delivery mode=prose) → AI 解读 block + 下单信息 block.
    - ``narration`` None (mode=card/none) → 下单信息 block only.

    Buttons carry ``action="trade_approval_resolve"`` (NOT ``approval_resolve`` —
    that is the assistant tool-call broker). The channel routes the click to the
    execution-side ``QueuedApprovalGate.approve``/``reject``.
    """
    approval_id = str(payload.get("approval_id") or "")
    task_id = str(payload.get("task_id") or "")
    intent_id = str(payload.get("intent_id") or "")
    timeout_seconds = int(payload.get("timeout_seconds") or 0)
    expires_at = str(payload.get("expires_at") or "")

    narration_text = str(narration or "").strip()
    elements: list[dict[str, Any]] = []
    if narration_text:
        # AI 解读 — advisory, separated from the authoritative facts (shared helper so
        # the terminal card renders the exact same block after a click).
        elements.extend(_ai_interpretation_elements(narration_text))
    else:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "实盘交易订单待人工审批，请确认是否下单："},
            }
        )
    # Deterministic 下单信息 — ALWAYS present, the execution-authoritative facts.
    elements.append(
        {"tag": "div", "text": {"tag": "lark_md", "content": "**📋 下单信息（成交以此为准）**"}}
    )
    elements.extend(_build_trade_approval_detail_elements(payload))

    expiry_text = ""
    if timeout_seconds:
        expiry_text = f"{timeout_seconds} 秒内未审批将自动过期，订单不会下单。"
    elif expires_at:
        expiry_text = f"超过 {expires_at} 未审批将自动过期，订单不会下单。"
    if expiry_text:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"<font color='grey'>{expiry_text}</font>",
                },
            }
        )
    # Fixed 免责 footnote (code-rendered, never LLM-authored) — the moment of a
    # real order is exactly where "决策与下单由你本人负责" must be unmissable.
    elements.extend(_risk_disclaimer_elements())
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**审批**"}})

    # The terminal card (after a click) is rebuilt from the button's callback
    # value alone — the click event carries no order facts otherwise. So carry
    # the display fields here, keeping 待审批 ↔ 终态 cards consistent. Rationale +
    # narration are capped so the callback value stays small. The narration is
    # display-ONLY (re-rendered as 终态 AI 解读); it NEVER reaches execution.
    rationale_brief = str(payload.get("rationale") or "")[:120]
    narration_brief = narration_text[:_APPROVAL_CALLBACK_NARRATION_MAX]

    def _button(label: str, decision: str, btn_type: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btn_type,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "action": "trade_approval_resolve",
                        "approval_id": approval_id,
                        "decision": decision,
                        "task_id": task_id,
                        "intent_id": intent_id,
                        "symbol": str(payload.get("symbol") or ""),
                        "symbol_name": str(payload.get("symbol_name") or ""),
                        "side": str(payload.get("action") or ""),
                        "notional": str(payload.get("notional") or ""),
                        "strategy_tag": str(payload.get("strategy_tag") or ""),
                        "signal_tag": str(payload.get("signal_tag") or ""),
                        "rationale": rationale_brief,
                        "created_at": str(payload.get("created_at") or ""),
                        "price_reference": str(payload.get("price_reference") or ""),
                        "order_type": str(payload.get("order_type") or ""),
                        "tif": str(payload.get("tif") or ""),
                        "exit_reason": str(payload.get("exit_reason") or ""),
                        "last_price": str(payload.get("last_price") or ""),
                        "pct_change": str(payload.get("pct_change") or ""),
                        "direction": str(payload.get("direction") or ""),
                        "narration": narration_brief,
                    },
                }
            ],
        }

    elements.append(
        _button_row(
            [
                _button("✅ 批准下单", "approve", "primary"),
                _button("❌ 拒绝", "reject", "danger"),
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "⚠️ 实盘交易审批"},
        },
        "body": {"elements": elements},
    }


def build_trade_approval_resolved_card(
    payload: dict[str, Any],
    *,
    decision: str,
    resolver: str = "",
) -> dict[str, Any]:
    """Terminal card shown after a trade approval is approved / rejected.

    Same order facts as ``build_trade_approval_card`` plus the resolver, no
    buttons. ``decision`` is ``"approve"`` (green 已批准) or anything else
    (grey 已拒绝). When the pending card carried an Agent narration, the SAME
    「🤖 AI 解读」block is re-rendered here (from ``payload["narration"]``, carried
    in the button callback value) so the operator's reasoning context survives the
    click instead of vanishing into a bare 已批准/已拒绝 notice.
    """
    approved = str(decision or "").strip().lower() in ("approve", "approved")
    if approved:
        template = "green"
        title = "✅ 实盘交易审批 · 已批准"
        status_line = "该订单已批准，将由调度补单链路下单，成交结果会另发卡片通知。"
    else:
        template = "grey"
        title = "🚫 实盘交易审批 · 已拒绝"
        status_line = "该订单已拒绝，不会下单。"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": status_line},
        }
    ]
    elements.extend(_ai_interpretation_elements(payload.get("narration")))
    elements.extend(_build_trade_approval_detail_elements(payload))
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>决议人：{resolver or '—'}</font>",
            },
        }
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def build_trade_approval_result_card(
    payload: dict[str, Any], *, outcome: str
) -> dict[str, Any]:
    """Receipt card pushed AFTER an approved order is dispatched to the broker.

    Distinct from ``build_trade_approval_resolved_card`` (which only confirms the
    审批 *decision*): the approve→fill happens later in the scheduler resume sweep,
    so without this an operator who saw 已批准 never learns whether the order
    ACTUALLY filled. This is the deterministic outcome receipt (no Agent, no
    buttons) — 已批准 must never be mistaken for 已成交.

    ``outcome``:
    - ``"filled"`` → green 已成交; shows the ACTUAL 成交数量/成交价/成交金额/成交时间
      from the persisted fill (``payload`` carries the ``fill_*`` display strings).
    - anything else (``"failed"`` / ``"abandoned"``) → red 未成交; shows the planned
      金额 + 失败原因 so the operator knows the order did NOT go through.

    All amounts are display strings already formatted by the caller (金额十进制 — the
    builder never re-floats them).
    """
    symbol = str(payload.get("symbol") or "—")
    symbol_name = str(payload.get("symbol_name") or "").strip()
    symbol_display = f"{symbol_name}（{symbol}）" if symbol_name else symbol
    action_label = _trade_action_label(str(payload.get("action") or ""))
    strategy_tag = str(payload.get("strategy_tag") or "—")
    task_id = str(payload.get("task_id") or "—")
    approval_id = str(payload.get("approval_id") or "—")
    run_id = str(payload.get("run_id") or "—")
    filled = str(outcome or "").strip().lower() == "filled"

    if filled:
        template = "green"
        title = "✅ 实盘下单结果 · 已成交"
        status_line = "已批准的订单已送达券商并成交，以下为实际成交信息（成交以此为准）："
        fields = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**标的**\n{symbol_display}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**\n{action_label}"}},
            {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**成交数量**\n{payload.get('fill_quantity') or '—'} 股"},
            },
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**成交价**\n{payload.get('fill_price') or '—'}"}},
            {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**成交金额**\n{payload.get('fill_amount') or '—'}"},
            },
            {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**成交时间**\n{payload.get('fill_time') or '—'}"},
            },
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**策略**\n{strategy_tag}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n{task_id}"}},
        ]
        footer = None
    else:
        template = "red"
        title = "⚠️ 实盘下单结果 · 未成交"
        status_line = "已批准的订单未能成交，请核查后决定是否重新下单。"
        fields = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**标的**\n{symbol_display}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**\n{action_label}"}},
            {
                "is_short": True,
                "text": {"tag": "lark_md", "content": f"**计划金额**\n{payload.get('notional') or '—'}"},
            },
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**策略**\n{strategy_tag}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n{task_id}"}},
        ]
        error_text = str(payload.get("error") or "").strip() or "执行适配器未返回成交（零成交 / 被拒）。"
        footer = {"tag": "div", "text": {"tag": "lark_md", "content": f"**失败原因**\n{error_text}"}}

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": status_line}},
        {"tag": "div", "fields": fields},
    ]
    if footer is not None:
        elements.append(footer)
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>审批号：{approval_id} · run_id：{run_id}</font>",
            },
        }
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def _pct_colored_html(value: Any) -> str:
    """Color a pct_change per A-share convention (涨红跌绿), ```` `` for unknown.

    Feishu ``lark_md`` accepts ``<font color='red|green|grey'>…</font>``. We honor
    the Chinese market's red=up / green=down so the digest reads correctly to an
    A-share operator (the western green=up convention would be misleading here).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "<font color='grey'>涨跌幅未知</font>"
    if f > 0:
        return f"<font color='red'>+{f:.2f}%</font>"
    if f < 0:
        return f"<font color='green'>{f:.2f}%</font>"
    return f"<font color='grey'>{f:.2f}%</font>"


def _signal_section_title(emoji: str, title: str) -> dict[str, Any]:
    """A bold, emoji-prefixed section header div (visual grouping in the card)."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": f"**{emoji} {title}**"}}


def _symbol_label(symbol: Any, symbol_names: dict[str, str] | None) -> str:
    """`工商银行（601398.SH）` when a display name is known, else the bare code.

    Mirrors the 持仓 block convention (`name（symbol）`): an A-share operator
    scanning the push must see the stock name, not only the opaque code.
    """
    code = str(symbol or "").strip()
    name = str((symbol_names or {}).get(code) or "").strip()
    return f"{name}（{code}）" if name else code


def _signal_market_elements(
    market: dict[str, Any], symbol_names: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Per-symbol 行情 block: 现价(涨跌幅) / 开盘·最高 / 最低·前收.

    Returns ``[]`` when there is nothing to show so the caller can drop the whole
    section instead of rendering an empty header. ``symbol_names`` (best-effort,
    may be ``None`` / partial) renders the Chinese name beside the code so the
    card is scannable by an A-share operator (parity with the 持仓 block).
    """
    elements: list[dict[str, Any]] = []
    for sym in sorted(market):
        info = market.get(sym) or {}
        if not isinstance(info, dict):
            continue
        lp = info.get("last_price")
        last_price = "—" if lp is None else str(lp)
        pct = _pct_colored_html(info.get("pct_change"))
        label = _symbol_label(sym, symbol_names)
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{label}**　{last_price}（{pct}）"},
            }
        )
        open_v = info.get("open")
        high_v = info.get("high")
        low_v = info.get("low")
        prev_v = info.get("prev_close")
        fields = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"开盘\n{open_v if open_v is not None else '—'}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"最高\n{high_v if high_v is not None else '—'}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"最低\n{low_v if low_v is not None else '—'}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"前收\n{prev_v if prev_v is not None else '—'}"}},
        ]
        elements.append({"tag": "div", "fields": fields})
    return elements


def _signal_diagnostic_elements(
    diags: dict[str, Any], symbol_names: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Per-symbol 判断 block: 方向 [标签] / 理由 (目标仓位).

    ``diags`` is ``details.signal_diagnostics`` — direction / tag / rationale
    straight from each symbol's Signal. ``[]`` when empty so the caller can drop
    the section. ``symbol_names`` renders the Chinese name beside the code.
    """
    elements: list[dict[str, Any]] = []
    for sym in sorted(diags):
        sig = diags.get(sym) or {}
        if not isinstance(sig, dict):
            continue
        direction = str(sig.get("direction") or "—")
        tag = str(sig.get("tag") or "").strip()
        tag_part = f"　`{tag}`" if tag else ""
        target = sig.get("target_exposure")
        target_part = f"　目标仓位 {target}" if target is not None else ""
        label = _symbol_label(sym, symbol_names)
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{label}**　{direction}{tag_part}{target_part}"},
            }
        )
        rationale = str(sig.get("rationale") or "").strip()
        if rationale:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": rationale}})
    return elements


def _signal_account_elements(post_cycle_account: dict[str, Any] | None) -> list[dict[str, Any]]:
    """账户 + 持仓 blocks from ``details.post_cycle_account``.

    Renders a 现金/总市值/总权益 fields block, then one block per holding
    (代码·名称 / 持仓·可用 / 成本·现价 / 市值). All monetary fields are display
    strings already (金额十进制) — the builder never re-floats them. Returns ``[]``
    when account data is absent so the caller can drop the section.
    """
    if not isinstance(post_cycle_account, dict):
        return []
    account = post_cycle_account.get("account") or {}
    cash = str(account.get("cash") or "—") if isinstance(account, dict) else "—"
    equity = str(account.get("equity") or "—") if isinstance(account, dict) else "—"
    total_mv = str(post_cycle_account.get("total_market_value") or "—")
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**现金**\n{cash}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**总市值**\n{total_mv}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**总权益**\n{equity}"}},
            ],
        }
    ]
    positions = post_cycle_account.get("positions") or []
    if isinstance(positions, list) and positions:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**📋 持仓**"}})
        for p in positions:
            if not isinstance(p, dict):
                continue
            symbol = str(p.get("symbol") or "—")
            name = str(p.get("name") or "").strip()
            title = f"{name}（{symbol}）" if name else symbol
            qty = p.get("quantity")
            available = p.get("available")
            cost = p.get("cost_price")
            last = p.get("last_price")
            mv = p.get("market_value")
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**"}}
            )
            elements.append(
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"持仓\n{qty if qty is not None else '—'}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"可用\n{available if available is not None else '—'}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"成本价\n{cost if cost is not None else '—'}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"现价\n{last if last is not None else '—'}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"市值\n{mv if mv is not None else '—'}"}},
                    ],
                }
            )
    return elements


def _intent_action_label(action: Any) -> str:
    a = str(action or "").strip().lower()
    return {"buy": "买入", "sell": "卖出"}.get(a, str(action or "—"))


def _signal_action_elements(
    details: dict[str, Any],
    digest: dict[str, Any],
    symbol_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """本轮动作 block: per-intent / per-fill lines, or the explicit no-signal notice.

    Never silently empty: an empty cycle renders「本轮无可执行信号，策略维持观望。」
    so the operator always sees the decision (§错误可见性 — a missing action must
    read as a deliberate 观望, not a dropped push). ``symbol_names`` renders the
    Chinese name beside each symbol code.
    """
    intents = details.get("position_intents") or []
    fills = details.get("fills") or []
    if not intents and not fills:
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": "本轮无可执行信号，策略维持观望。"}}
        ]
    lines: list[str] = []
    for it in intents[:20]:
        if not isinstance(it, dict):
            continue
        sym = it.get("symbol", "")
        label = _symbol_label(sym, symbol_names)
        act = _intent_action_label(it.get("action") or it.get("side"))
        amt = it.get("amount")
        amt_part = f" {amt}" if amt is not None else ""
        rat = it.get("rationale") or it.get("signal_tag") or ""
        rat_part = f" — {rat}" if rat else ""
        line = f"- {label} {act}{amt_part}{rat_part}".rstrip()
        if it.get("pending_approval"):
            line += "（待审批）"
        lines.append(line)
    for f in fills[:20]:
        if not isinstance(f, dict):
            continue
        side = _intent_action_label(f.get("side"))
        label = _symbol_label(f.get("symbol", ""), symbol_names)
        lines.append(
            f"- {label} {side} {f.get('quantity', '')}@{f.get('price', '')}"
        )
    return [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}]


def build_signal_digest_card(
    *,
    trigger_name: str,
    digest: dict[str, Any] | None,
    processed_at: str = "",
    no_signal_mode: str = "brief",
    prose_mode: bool = False,
    narration_sections: dict[str, str] | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    task_name: str = "",
    symbol_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rich multi-section Feishu card for a Trigger's strategy-signal digest.

    Replaces the plain single-markdown-blob card (``build_complete_card(text)``)
    for Feishu channel pushes. Renders the deterministic, authoritative facts
    straight from ``cycle_runs.details`` — 行情 / 判断 / 账户 / 持仓 / 本轮动作 —
    as CardKit 2.0 components (themed header, ``div.fields`` two-column blocks,
    colored 涨跌幅, section headers). All numbers come from the persisted digest
    (金额十进制 strings); nothing is re-floated or invented.

    Visibility rules mirror :func:`render_trigger_digest`:
    - ``cycle_failed`` → red 异常 header + failure message, no fact sections.
    - ``no_signal_mode="full"`` OR ``prose_mode`` OR actionable (intents/fills)
      → full sections (行情/判断/账户/持仓/动作).
    - ``no_signal_mode="brief"`` (card mode) with no action → compact card
      (header + 处理时间 + 本轮动作 观望 notice + footer).

    ``task_name`` / ``symbol_names`` carry the operator-facing 任务名 and 股票名称
    (resolved best-effort by the caller). The task name shows as a subtitle under
    the header; each stock name renders beside its code in 行情/判断/本轮动作
    (parity with the 持仓 block). Missing values degrade to the bare trigger name
    / symbol code — never raise at the display edge.

    ``narration_sections`` (prose mode only) carries the Agent-authored section
    bodies. They are rendered as a collapsed「🤖 AI 解读」panel — advisory, kept
    SEPARATE from the deterministic facts below (same separation discipline as
    :func:`build_trade_approval_card`'s AI 解读 block; a hallucinated number
    must read as opinion, never as the signal).
    """
    digest = digest or {}
    details = digest.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    intents = details.get("position_intents") or []
    fills = details.get("fills") or []
    actionable = bool(intents) or bool(fills) or bool(digest.get("submitted_count"))
    failed = bool(digest.get("cycle_failed"))

    # ---- header theme + title -------------------------------------------
    if failed:
        template = "red"
        title = f"⚠️ {trigger_name} · 策略信号异常"
    elif actionable:
        template = "green"
        title = f"📈 {trigger_name} · 策略信号"
    else:
        template = "blue"
        title = f"📊 {trigger_name} · 策略信号"

    elements: list[dict[str, Any]] = []

    # ---- 任务名 subtitle (operator-facing, not the opaque task_id) --------
    if task_name:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"<font color='grey'>任务：{task_name}</font>"},
            }
        )

    # ---- 处理时间 --------------------------------------------------------
    if processed_at:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"<font color='grey'>处理时间：{processed_at}（北京时间）</font>"},
            }
        )

    # ---- 失败：直接给出原因，不渲染行情/账户等事实段 -----------------------
    if failed:
        msg = digest.get("failure_message") or digest.get("status") or "运行失败"
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='red'>本轮运行失败：{msg}</font>"}}
        )
    else:
        # ---- AI 解读（prose 模式，折叠，仅供参考）--------------------------
        if narration_sections:
            narration_lines: list[str] = []
            for key, label in (
                ("market", "行情"),
                ("judgement", "判断"),
                ("account", "账户"),
                ("action", "本轮动作"),
            ):
                body = str(narration_sections.get(key) or "").strip()
                if body:
                    narration_lines.append(f"**{label}**\n{body}")
            if narration_lines:
                elements.append(
                    {
                        "tag": "collapsible_panel",
                        "expanded": False,
                        "header": {
                            "title": {"tag": "plain_text", "content": "🤖 AI 解读（仅供参考）"},
                        },
                        "elements": [{"tag": "markdown", "content": _optimize_markdown("\n\n".join(narration_lines))}],
                    }
                )

        show_full = (no_signal_mode == "full") or prose_mode or actionable

        if show_full:
            market_els = _signal_market_elements(
                details.get("market_snapshot") or {}, symbol_names
            )
            if market_els:
                elements.append({"tag": "hr"})
                elements.append(_signal_section_title("📈", "行情"))
                elements.extend(market_els)
            else:
                elements.append({"tag": "hr"})
                elements.append(_signal_section_title("📈", "行情"))
                elements.append(
                    {"tag": "div", "text": {"tag": "lark_md", "content": "<font color='grey'>行情数据缺失</font>"}}
                )

            diag_els = _signal_diagnostic_elements(
                details.get("signal_diagnostics") or {}, symbol_names
            )
            if diag_els:
                elements.append({"tag": "hr"})
                elements.append(_signal_section_title("🧭", "判断"))
                elements.extend(diag_els)

            account_els = _signal_account_elements(details.get("post_cycle_account"))
            if account_els:
                elements.append({"tag": "hr"})
                elements.append(_signal_section_title("💰", "账户"))
                elements.extend(account_els)

        # 本轮动作 — always present (compact card shows only this).
        elements.append({"tag": "hr"})
        elements.append(_signal_section_title("⚡", "本轮动作"))
        elements.extend(_signal_action_elements(details, digest, symbol_names))

    # ---- footer: run_id / task (run_id 贯穿，traceability) ----------------
    footer_bits: list[str] = []
    if run_id:
        footer_bits.append(f"run_id：{run_id}")
    if task_id:
        # Show the human task name when known; keep the opaque task_id for
        # traceability (debug / CLI lookups key off it).
        if task_name:
            footer_bits.append(f"task：{task_name}（{task_id}）")
        else:
            footer_bits.append(f"task：{task_id}")
    run_mode = str(digest.get("run_mode") or "").strip()
    if run_mode:
        footer_bits.append(f"模式：{run_mode}")
    if footer_bits:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"<font color='grey'>{' · '.join(footer_bits)}</font>"},
            }
        )

    # Fixed 免责 footnote (code-rendered, never LLM-authored).
    elements.extend(_risk_disclaimer_elements())

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def build_ask_user_card(pending: dict[str, Any]) -> dict[str, Any]:
    """Build the interactive card for an ``ask_user_question`` pending payload.

    ``pending`` is the dict the tool persisted in
    ``session.config["pending_user_question"]``:
    ``{question_id, question, header?, options: [{label, description?}],
    multi_select}``. Each option renders as a button whose click the channel
    turns into ``/ask_user <question_id> <label>``; a free-text input +
    submit (``ask_user_text``) is always offered as the escape hatch.
    """
    question_id = str(pending.get("question_id") or "")
    question = str(pending.get("question") or "")
    header_tag = str(pending.get("header") or "").strip()
    options = [opt for opt in (pending.get("options") or []) if isinstance(opt, dict)]

    title = "❓ 需要你的选择"
    if header_tag:
        title = f"❓ {header_tag}"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": question},
        }
    ]

    option_buttons: list[dict[str, Any]] = []
    for option in options:
        label = str(option.get("label") or "").strip()
        if not label:
            continue
        option_buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if not option_buttons else "default",
                "value": {
                    "action": "ask_user_select",
                    "ask_user_id": question_id,
                    "option_label": label,
                },
            }
        )
    if option_buttons:
        elements.extend(_button_group(option_buttons))

    descriptions = [
        f"- **{opt.get('label')}**：{opt.get('description')}"
        for opt in options
        if opt.get("description")
    ]
    if descriptions:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(descriptions)},
            }
        )

    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "input",
            "label": {"tag": "plain_text", "content": ""},
            "element_id": f"input_{question_id}",
            "placeholder": {
                "tag": "plain_text",
                "content": "或直接输入你的回答…",
            },
        }
    )
    elements.append(
        _button_row(
            [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "提交"},
                    "type": "default",
                    "value": {"action": "ask_user_text", "ask_user_id": question_id},
                }
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": elements,
        },
    }


def build_ask_user_answered_card(
    answer: str, *, submitted: bool = False, header_tag: str = ""
) -> dict[str, Any]:
    """Build the terminal card that replaces an ask_user card after a click.

    No buttons / no input — the card is locked so the operator cannot click it
    again. ``submitted`` distinguishes a free-text submit from an option click.
    """
    title = "✅ 已回答" if submitted else "✅ 已选择"
    if header_tag:
        title = f"✅ {header_tag}"
    safe_answer = str(answer or "").strip() or "(空)"
    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**你的回答：**\n{safe_answer}"},
        }
    ]
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": elements,
        },
    }


_MONITOR_CARD_TEMPLATE = {
    "limit_up": "red",
    "limit_up_seal_shrink": "red",
    "limit_down": "green",
    "limit_down_seal_shrink": "green",
    "limit_up_open": "orange",
    "limit_down_open": "turquoise",
    "composite": "blue",
}


def _fmt_num(value: Any) -> str:
    """Format a price/volume for display; '—' when missing. Never raises."""
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.3f}".rstrip("0").rstrip(".")


def build_stock_alert_card(
    *,
    symbol: str,
    display_name: str | None,
    condition_label: str,
    condition_name: str,
    triggered_at: str,
    last_price: float | None = None,
    limit_price: float | None = None,
    seal_peak: float | None = None,
    seal_now: float | None = None,
    drop_pct: float | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Feishu CardKit 2.0 card for a fired 盯盘 alert (股票智能监控告警).

    Header theme follows A-share convention (涨红跌绿; 打开 = 橙/青 weakening). All
    numbers come straight from the detector diagnostics — nothing is re-computed
    here. The 封单 before/after block renders only for the 大减 presets (when
    ``seal_peak``/``seal_now`` are present).
    """
    template = _MONITOR_CARD_TEMPLATE.get(condition_name, "blue")
    label = _symbol_label(symbol, {symbol: display_name} if display_name else None)
    title = f"📡 {condition_label} · {label}"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"<font color='grey'>触发时间：{triggered_at}（北京时间）</font>",
            },
        },
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**现价**\n{_fmt_num(last_price)}"},
                },
                {
                    "is_short": True,
                    "text": {"tag": "lark_md", "content": f"**涨跌停价**\n{_fmt_num(limit_price)}"},
                },
            ],
        },
    ]

    if seal_peak is not None or seal_now is not None:
        drop_text = f"{drop_pct * 100:.1f}%" if isinstance(drop_pct, (int, float)) else "—"
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**封单峰值**\n{_fmt_num(seal_peak)}"},
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**当前封单**\n{_fmt_num(seal_now)}"},
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md", "content": f"**封单减少**\n{drop_text}"},
                    },
                ],
            }
        )

    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"<font color='grey'>标的：{symbol}</font>"},
        }
    )

    # Fixed 免责 footnote (code-rendered, never LLM-authored).
    elements.extend(_risk_disclaimer_elements())

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def build_thinking_card(reasoning_text: str | None = None) -> dict[str, Any]:
    """Build a simple placeholder card showing 'thinking' state."""
    card = copy.deepcopy(THINKING_CARD_JSON)
    card["body"] = {
        "elements": [
            {
                "tag": "markdown",
                "content": _optimize_markdown(reasoning_text or "思考中... / Thinking..."),
                "element_id": STREAMING_ELEMENT_ID,
            }
        ],
    }
    return card


def build_tool_call_card(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Build a standalone card for one assistant tool call."""
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "body": {
            "elements": [_build_tool_call_panel(tool_call)],
        },
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_tool_use_pending_panel() -> dict[str, Any]:
    """Build a collapsible panel for pending tool use (grey header, no elements)."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🔧 工具使用 / Tool Use",
            },
        },
        "elements": [],
    }


def _build_reasoning_panel(
    reasoning_text: str,
    *,
    expanded: bool,
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    suffix = f" · {_format_elapsed(elapsed_ms)}" if elapsed_ms is not None else ""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"思考 / Thinking{suffix}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": _optimize_markdown(reasoning_text),
            }
        ],
    }


def _build_tool_call_panel(tool_call: dict[str, Any]) -> dict[str, Any]:
    name = str(tool_call.get("name") or "tool")
    status = str(tool_call.get("status") or "pending")
    category = str(tool_call.get("category") or "工具")
    result = tool_call.get("result") if isinstance(tool_call.get("result"), dict) else None
    status_text = {
        "pending": "等待中",
        "running": "调用中",
        "completed": "已完成",
        "error": "失败",
    }.get(status, status)
    elements = [
        {
            "tag": "markdown",
            "content": "**输入参数**\n```json\n" + _json_preview(tool_call.get("input")) + "\n```",
        }
    ]
    if result is not None:
        output = result.get("output")
        output_title = "**输出结果**"
        if result.get("is_error"):
            output_title = "**输出结果（错误）**"
        elements.append(
            {
                "tag": "markdown",
                "content": output_title + "\n```json\n" + _json_preview(output) + "\n```",
            }
        )
    return {
        "tag": "collapsible_panel",
        "expanded": status in {"running", "error"},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"{category} · {name} · {status_text}",
            },
        },
        "elements": elements,
    }


def _build_tool_use_panel() -> dict[str, Any]:
    """Build a collapsible panel for tool use with placeholder content."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "🔧 工具使用 / Tool Use",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "Tools were used during this execution.",
            }
        ],
    }


def _optimize_markdown(text: str) -> str:
    """Normalize markdown for Feishu cards without stripping useful formatting."""
    normalized = re.sub(r"\n{4,}", "\n\n\n", str(text)).strip()
    return normalized[:30000] if len(normalized) > 30000 else normalized


def _json_preview(value: Any) -> str:
    import json

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value[:8000]
        value = parsed
    try:
        text = json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        text = str(value)
    return text[:8000]


def _format_elapsed(ms: int | None) -> str:
    """Format milliseconds as human-readable string.

    Examples:
        3500 -> "3.5s"
        90000 -> "1m 30s"
        45000 -> "45s"
    """
    if ms is None:
        return ""
    total_seconds = ms / 1000
    if total_seconds < 60:
        if ms % 1000 == 0:
            return f"{int(total_seconds)}s"
        return f"{total_seconds:.1f}s"
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)
    return f"{minutes}m {seconds}s"
