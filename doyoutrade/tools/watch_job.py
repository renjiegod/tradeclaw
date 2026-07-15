"""watch_job — register a completion wake-up for a long-running job.

Instead of blocking on ``backtest watch``, the assistant registers a watch
and ends its turn; when the job reaches a terminal status the
``JobWatchService`` (doyoutrade/assistant/job_watcher.py) composes a result
summary and pushes it into THIS session as a ``[job-completed]`` wake-up.

In-process (not CLI) because the watch is bound to the calling session —
``session_id`` / ``agent_id`` are auto-filled by the registry, never
self-reported by the model.
"""

from __future__ import annotations

from typing import Any

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import format_error_text, format_unknown_args

_TERMINAL = frozenset({"completed", "failed", "stopped"})


class WatchJobTool(OperationHandler):
    name = "watch_job"
    description = (
        "登记一个后台任务（当前支持 backtest run）的完成提醒：任务到达终态时，"
        "系统会自动把结果摘要推送回当前会话。用于代替阻塞式的 backtest watch——"
        "登记后即可结束本轮回复，告诉用户'回测完成后会自动通知'。"
    )
    category = "agent"
    requires_session_id = True
    requires_calling_agent_id = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要监视的 backtest run/job id（backtest run 返回的 id）。",
            },
            "note": {
                "type": "string",
                "description": (
                    "可选备注：你为什么关心这个任务、完成后该重点看什么。"
                    "唤醒时会原样交还给你，用于恢复上下文。"
                ),
            },
        },
        "required": ["job_id"],
    }

    def __init__(
        self,
        watch_repository: Any | None = None,
        run_repository: Any | None = None,
    ) -> None:
        self._watch_repository = watch_repository
        self._run_repository = run_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        session_id = kwargs.pop("session_id", None)
        agent_id = kwargs.pop("agent_id", None)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "session_id": session_id,
            "input_keys": sorted(kwargs.keys()),
        }

        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}."
                f"{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {**base_payload, "error": contract.error},
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

        job_id = str(kwargs.get("job_id") or "").strip()
        note = str(kwargs.get("note") or "").strip() or None
        if not job_id:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "validation_error", "field": "job_id"},
            )
            return ToolResult(
                text=format_error_text("validation_error", "job_id must be a non-empty string"),
                is_error=True,
            )

        if (
            not session_id
            or not agent_id
            or self._watch_repository is None
            or self._run_repository is None
        ):
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "watch_unwired",
                    "hint": (
                        "watch_job needs watch_repository + run_repository wiring "
                        "(build_default_tool_registry) and a session-bound invocation"
                    ),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "watch_unwired",
                    "this runtime cannot register job watches; fall back to "
                    "`doyoutrade-cli backtest watch` instead.",
                ),
                is_error=True,
            )

        try:
            run = await self._run_repository.get(job_id)
        except Exception as exc:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": "job_lookup_failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "job_lookup_failed", f"could not look up job {job_id!r}: {exc}"
                ),
                is_error=True,
            )
        if run is None:
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {**base_payload, "error_code": "job_not_found", "job_id": job_id},
            )
            return ToolResult(
                text=format_error_text(
                    "job_not_found",
                    f"no backtest run found for id {job_id!r}; check the id "
                    "returned by `backtest run`.",
                ),
                is_error=True,
            )

        status = str(run.get("status") or "")
        if status in _TERMINAL:
            # Nothing to wait for — steer the model to read the report now
            # instead of registering a watch that would fire immediately.
            await emit_debug_event(
                f"operation_{self.name}.shortcircuit",
                {**base_payload, "job_id": job_id, "job_status": status},
            )
            return ToolResult(
                text=(
                    f'{{"status":"already_terminal","job_id":"{job_id}",'
                    f'"job_status":"{status}"}}\n'
                    f"该任务已是终态（{status}），无需登记提醒。直接用 "
                    f"`doyoutrade-cli backtest summary --run-id {job_id}` 读取报告。"
                ),
            )

        watch = await self._watch_repository.create(
            session_id=session_id,
            agent_id=agent_id,
            job_id=job_id,
            job_kind="backtest",
            note=note,
        )
        await emit_debug_event(
            f"operation_{self.name}.created",
            {
                **base_payload,
                "watch_id": watch["watch_id"],
                "job_id": job_id,
                "job_status": status,
                "has_note": bool(note),
            },
        )
        return ToolResult(
            text=(
                f'{{"status":"created","watch_id":"{watch["watch_id"]}",'
                f'"job_id":"{job_id}","job_status":"{status}"}}\n'
                "已登记完成提醒。现在结束本轮回复：告诉用户任务在后台运行、"
                "完成后会自动收到结果摘要即可。不要再轮询、不要 backtest watch、"
                "不要重复调用本工具。"
            ),
        )
