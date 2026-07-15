"""record_decision_signal — persist an assistant-made trading decision.

Writes one row to ``decision_signals`` (source ``assistant``, attributed to
the calling session via the auto-filled ``session_id``) so a conversational
"我建议买入 600519，5 天目标 1800" becomes a durable, later-verifiable signal.
Idempotent: the repository dedupes on
``(session_id, symbol, action, horizon)`` — a repeat call returns the
existing row with ``deduped: true`` instead of double-recording.

NOT yet registered in ``build_default_tool_registry`` (tools/__init__.py's
registry section is owned elsewhere); see the feature-5 delivery notes for
the exact registration diff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

_ACTIONS = (
    "buy",
    "sell",
    "hold",
    "add",
    "reduce",
    "watch",
    "take_profit",
    "stop_loss",
)
_PRICE_FIELDS = ("entry_low", "entry_high", "stop_loss", "target_price")


class RecordDecisionSignalTool(OperationHandler):
    name = "record_decision_signal"
    description = (
        "把你在对话中做出的交易决策（买入/卖出/观望等）落库为一条可追溯、"
        "可回测验证的决策信号（dsig-…）。symbol 必须是 canonical 代码"
        "（如 600519.SH，先经 stock lookup）；价格字段一律十进制字符串。"
        "同一会话对同一 (symbol, action, horizon) 重复调用会去重返回已有信号。"
        "之后可用 `doyoutrade-cli decision-signal evaluate <dsig-…>` 验证命中率。"
    )
    category = "agent"
    # Session attribution: the dispatcher auto-fills ``session_id`` from the
    # calling session (same mechanism as ask_user_question) — the model never
    # recites its own session id.
    requires_session_id = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Canonical symbol, e.g. 600519.SH (必填, 先经 stock lookup).",
            },
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "决策动作（八态）。",
            },
            "confidence": {
                "type": "number",
                "description": "置信度 0-1。可选。",
            },
            "score": {
                "type": "number",
                "description": "打分（自定义量纲）。可选。",
            },
            "horizon": {
                "type": "string",
                "description": "验证窗口，如 '5d'（默认 5d）。",
            },
            "entry_low": {
                "type": "string",
                "description": "建仓区间下沿，十进制字符串，如 '1688.00'。可选。",
            },
            "entry_high": {
                "type": "string",
                "description": "建仓区间上沿，十进制字符串。可选。",
            },
            "stop_loss": {
                "type": "string",
                "description": "止损价，十进制字符串。可选。",
            },
            "target_price": {
                "type": "string",
                "description": "目标价，十进制字符串。可选。",
            },
            "reason": {
                "type": "string",
                "description": "决策理由（自由文本）。可选。",
            },
            "expires_in_days": {
                "type": "integer",
                "description": "N 天后信号自动过期（懒过期）。可选。",
            },
            "metadata": {
                "type": "object",
                "description": "附加结构化上下文（JSON object）。可选。",
            },
        },
        "required": ["symbol", "action"],
    }
    coercion_rules = (
        SchemaCoercion(field="metadata", declared_type="object"),
    )

    def __init__(self, decision_signal_repository: Any | None = None) -> None:
        self._repository = decision_signal_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(k for k in kwargs.keys() if k != "session_id"),
        }
        session_id = kwargs.pop("session_id", None)

        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}."
                f"{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {**base_payload, "session_id": session_id, "error": contract.error},
            )
            if contract.error_kind == "unknown_arguments":
                text = format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = format_error_text(
                    "validation_error",
                    str(contract.error.get("message") or "validation failed"),
                )
            return ToolResult(text=text, is_error=True)
        kwargs = contract.kwargs

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "session_id": session_id, "error": coercion.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(coercion.error.get("error_code") or "invalid_metadata_json"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs

        problem = self._validate_domain(kwargs)
        if problem is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "session_id": session_id, "error": problem},
            )
            return ToolResult(
                text=format_error_text("validation_error", problem),
                is_error=True,
            )

        if not session_id or self._repository is None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "session_id": session_id,
                    "error_code": "decision_signal_unwired",
                    "hint": (
                        "record_decision_signal needs the decision signal repository "
                        "(RecordDecisionSignalTool(decision_signal_repository=...)) and a "
                        "session-bound invocation"
                    ),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "decision_signal_unwired",
                    "this runtime has no decision-signal persistence wiring; "
                    "the signal cannot be recorded here.",
                ),
                is_error=True,
            )

        horizon = str(kwargs.get("horizon") or "5d").strip() or "5d"
        expires_at = None
        expires_in_days = kwargs.get("expires_in_days")
        if expires_in_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=int(expires_in_days))
            ).replace(tzinfo=None)
        fields: dict[str, Any] = {
            "session_id": session_id,
            "source": "assistant",
            "symbol": str(kwargs["symbol"]).strip(),
            "action": str(kwargs["action"]).strip().lower(),
            "horizon": horizon,
            "confidence": kwargs.get("confidence"),
            "score": kwargs.get("score"),
            "reason": str(kwargs["reason"]).strip() if kwargs.get("reason") else None,
            "expires_at": expires_at,
            "metadata_json": kwargs.get("metadata"),
        }
        for field in _PRICE_FIELDS:
            value = kwargs.get(field)
            fields[field] = str(Decimal(str(value))) if value is not None else None

        try:
            snapshot, created = await self._repository.create_if_absent(**fields)
        except ValueError as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "session_id": session_id,
                    "error_code": "validation_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text("validation_error", str(exc)),
                is_error=True,
            )
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "session_id": session_id,
                    "error_code": "decision_signal_write_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "hint": "check decision_signals table and DB connectivity",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "decision_signal_write_failed",
                    f"could not persist the decision signal: {exc}",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "session_id": session_id,
                "signal_id": snapshot.id,
                "symbol": snapshot.symbol,
                "action": snapshot.action,
                "horizon": snapshot.horizon,
                "created": created,
            },
        )
        payload = {
            "status": "created" if created else "ok",
            "deduped": not created,
            "signal_id": snapshot.id,
            "symbol": snapshot.symbol,
            "action": snapshot.action,
            "horizon": snapshot.horizon,
            "signal_status": snapshot.status,
            "expires_at": snapshot.expires_at.isoformat() if snapshot.expires_at else None,
        }
        summary = (
            f"决策信号已落库 signal_id={snapshot.id}（{snapshot.symbol} {snapshot.action}, "
            f"horizon={snapshot.horizon}）。"
            if created
            else (
                f"该信号已存在（去重返回）signal_id={snapshot.id}（{snapshot.symbol} "
                f"{snapshot.action}, horizon={snapshot.horizon}）。"
            )
        )
        return ToolResult(text=append_json_payload(summary, payload))

    def _validate_domain(self, kwargs: dict[str, Any]) -> str | None:
        symbol = kwargs.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return f"symbol must be a non-empty string, got {type(symbol).__name__}: {symbol!r}"
        action = kwargs.get("action")
        if not isinstance(action, str) or action.strip().lower() not in _ACTIONS:
            return f"action must be one of {list(_ACTIONS)}, got {action!r}"
        for field in ("confidence", "score"):
            value = kwargs.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return f"{field} must be a number, got {type(value).__name__}: {value!r}"
        confidence = kwargs.get("confidence")
        if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
            return f"confidence must be within [0, 1], got {confidence!r}"
        horizon = kwargs.get("horizon")
        if horizon is not None:
            text = str(horizon).strip().lower()
            digits = text[:-1] if text.endswith("d") else text
            if not digits.isdigit() or int(digits) < 1:
                return f"horizon must look like '5d' (positive day count), got {horizon!r}"
        for field in _PRICE_FIELDS:
            value = kwargs.get(field)
            if value is None:
                continue
            if isinstance(value, bool):
                return f"{field} must be a decimal string or number, got bool: {value!r}"
            try:
                Decimal(str(value))
            except (InvalidOperation, ValueError):
                return (
                    f"{field} must be a decimal string like '1688.00', "
                    f"got {type(value).__name__}: {value!r}"
                )
        expires_in_days = kwargs.get("expires_in_days")
        if expires_in_days is not None and (
            isinstance(expires_in_days, bool)
            or not isinstance(expires_in_days, int)
            or expires_in_days < 1
        ):
            return (
                f"expires_in_days must be a positive integer, "
                f"got {type(expires_in_days).__name__}: {expires_in_days!r}"
            )
        metadata = kwargs.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return f"metadata must be a JSON object, got {type(metadata).__name__}: {metadata!r}"
        return None


__all__ = ["RecordDecisionSignalTool"]
