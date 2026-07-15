"""ask_user_question — present one structured, clickable question to the user.

Non-blocking semantics: the tool records the pending question in
``session.config["pending_user_question"]`` and returns immediately; the
model is instructed to END its turn. Channels render the options (Feishu
interactive card buttons, web option buttons); a click arrives as the next
user message via the ``/ask_user <question_id> <answer>`` protocol, which
``AssistantService`` rewrites into readable text and correlates back to the
pending question. A free-typed user message also answers (and clears) the
pending question — buttons are a convenience, not a gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import format_error_text, format_unknown_args

_MIN_OPTIONS = 2
_MAX_OPTIONS = 4
_MAX_HEADER_CHARS = 12


class AskUserQuestionTool(OperationHandler):
    name = "ask_user_question"
    description = (
        "向用户提出一个带 2-4 个选项的结构化问题（渲染为可点击按钮）。"
        "非阻塞：调用后立即返回，你必须结束本轮回复等待用户选择；"
        "用户的选择（或自由输入）会作为下一条用户消息到达。"
        "只在确实需要用户拍板、且选项可枚举时使用；开放式问题直接在正文里问。"
    )
    category = "agent"
    requires_session_id = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": {
                "type": "string",
                "description": "完整的问题文本，以问号结尾。",
            },
            "header": {
                "type": "string",
                "description": "极短的分类标签（≤12 字符），例如 '回测区间'。可选。",
            },
            "options": {
                "type": "array",
                "minItems": _MIN_OPTIONS,
                "maxItems": _MAX_OPTIONS,
                "description": "2-4 个互斥选项。",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "选项显示文本（1-5 个词，将原样作为用户答案回传）。",
                        },
                        "description": {
                            "type": "string",
                            "description": "该选项的含义 / 后果说明。可选。",
                        },
                    },
                    "required": ["label"],
                },
            },
            "multi_select": {
                "type": "boolean",
                "description": "允许多选（当前渠道按单选按钮渲染；用户也可自由输入组合）。默认 false。",
            },
        },
        "required": ["question", "options"],
    }
    coercion_rules = (
        SchemaCoercion(field="options", declared_type="array", item_type=dict),
        SchemaCoercion(field="multi_select", declared_type="boolean"),
    )

    def __init__(self, assistant_repository: Any | None = None) -> None:
        # Required at runtime: the pending question lives in
        # ``session.config`` via ``update_session_config``. Calling the tool
        # without this wiring is a hard, structured error.
        self._assistant_repository = assistant_repository

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
                    str(coercion.error.get("error_code") or "coercion_error"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs

        problem = self._validate_shape(kwargs)
        if problem is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "session_id": session_id, "error": problem},
            )
            return ToolResult(
                text=format_error_text("validation_error", problem),
                is_error=True,
            )

        if not session_id or self._assistant_repository is None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "session_id": session_id,
                    "error_code": "ask_user_unwired",
                    "hint": (
                        "ask_user_question needs the assistant session repository "
                        "(build_default_tool_registry assistant_repository=...) and a "
                        "session-bound invocation"
                    ),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "ask_user_unwired",
                    "this runtime has no session-state wiring; the question cannot "
                    "be presented to a user here. Ask in plain text instead.",
                ),
                is_error=True,
            )

        question_id = f"uq-{uuid4().hex[:8]}"
        pending = {
            "question_id": question_id,
            "question": str(kwargs["question"]).strip(),
            "header": str(kwargs.get("header") or "").strip()[:_MAX_HEADER_CHARS] or None,
            "options": [
                {
                    "label": str(option["label"]).strip(),
                    "description": str(option.get("description") or "").strip() or None,
                }
                for option in kwargs["options"]
            ],
            "multi_select": bool(kwargs.get("multi_select") or False),
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._assistant_repository.update_session_config(
                session_id, {"pending_user_question": pending}
            )
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "session_id": session_id,
                    "error_code": "ask_user_state_write_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "hint": "session config write failed; check assistant_sessions table",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "ask_user_state_write_failed",
                    f"could not persist the pending question: {exc}",
                ),
                is_error=True,
            )

        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "session_id": session_id,
                "question_id": question_id,
                "option_count": len(pending["options"]),
                "multi_select": pending["multi_select"],
            },
        )
        labels = " / ".join(option["label"] for option in pending["options"])
        return ToolResult(
            text=(
                f"问题已提交给用户（question_id={question_id}，选项：{labels}）。"
                "现在结束本轮回复：用一句话告诉用户你在等待他的选择即可，"
                "不要复述所有选项、不要继续追问、不要再调用本工具。"
                "用户的选择（或他自由输入的回答）会作为下一条用户消息到达。"
            )
        )

    def _validate_shape(self, kwargs: dict[str, Any]) -> str | None:
        question = kwargs.get("question")
        if not isinstance(question, str) or not question.strip():
            return f"question must be a non-empty string, got {type(question).__name__}: {question!r}"
        options = kwargs.get("options")
        if not isinstance(options, list) or not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
            count = len(options) if isinstance(options, list) else options
            return (
                f"options must be a list of {_MIN_OPTIONS}-{_MAX_OPTIONS} items, got: {count!r}"
            )
        seen: set[str] = set()
        for index, option in enumerate(options):
            if not isinstance(option, dict):
                return f"options[{index}] must be an object, got {type(option).__name__}"
            label = option.get("label")
            if not isinstance(label, str) or not label.strip():
                return f"options[{index}].label must be a non-empty string, got {label!r}"
            folded = label.strip().casefold()
            if folded in seen:
                return f"options[{index}].label duplicates another option: {label!r}"
            seen.add(folded)
        return None
