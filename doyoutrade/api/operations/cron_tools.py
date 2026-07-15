from __future__ import annotations

from typing import Any

from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._cron_schedule import (
    CRON_TOOL_TIMEZONE,
    NormalizedSchedule,
    ScheduleValidationError,
    normalize_schedule,
)
from doyoutrade.tools._pagination import append_pagination_hint
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)
from doyoutrade.debug import emit_debug_event


_PRE_ACTION_DESCRIPTION = (
    "Optional pre-action. Shape: {\"kind\": <str>, \"params\": <object>}. "
    "Currently registered kinds: 'noop'. "
    "Pass null to clear the pre-action on update_cron_job."
)


_SCHEDULE_DESCRIPTION = (
    "Tagged-union timing spec. Required fields depend on `kind`:\n"
    "  - kind='once_at': fires once at an absolute Asia/Shanghai instant. "
    "Provide EITHER `at` (ISO-8601 timestamp; bare timestamps assumed "
    "Asia/Shanghai) OR `delay_seconds` (integer >=0, relative to currentTime). "
    "Sub-minute precision is rounded UP to the next whole minute. "
    "Example: {kind:'once_at', delay_seconds:30} for '30秒后'.\n"
    "  - kind='every': recurring sub-day interval. `every_seconds` must be "
    "a multiple of 60. Supported sub-hour minute values: 1,2,3,4,5,6,10,12,"
    "15,20,30. Supported hour values: 1,2,3,4,6,8,12,24.\n"
    "  - kind='cron': recurring 5-field cron expression in Asia/Shanghai. "
    "Use this only when 'every' can't express the pattern (e.g. weekdays-only "
    "at 9am: {kind:'cron', expr:'0 9 * * 1-5'}).\n"
    "Never put a sub-minute delay into kind='every' — use kind='once_at' "
    "with delay_seconds instead."
)


_SCHEDULE_FIELD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": _SCHEDULE_DESCRIPTION,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["once_at", "every", "cron"],
            "description": "Schedule kind — see field-level description.",
        },
        "at": {
            "type": "string",
            "description": (
                "[kind=once_at] ISO-8601 absolute timestamp. Bare strings "
                "(no offset) are interpreted as Asia/Shanghai."
            ),
        },
        "delay_seconds": {
            "type": "integer",
            "description": (
                "[kind=once_at] Seconds from currentTime to fire. "
                "Sub-minute values round UP to the next whole minute."
            ),
        },
        "every_seconds": {
            "type": "integer",
            "description": (
                "[kind=every] Interval in seconds. Must be a multiple of "
                "60; minimum 60 (cron has minute resolution)."
            ),
        },
        "expr": {
            "type": "string",
            "description": (
                "[kind=cron] 5-field cron expression, e.g. '0 9 * * 1-5'. "
                "Interpreted as Asia/Shanghai."
            ),
        },
    },
    "required": ["kind"],
}


def _validate_pre_action(value: Any) -> dict[str, Any] | None:
    """Validate a non-None pre_action payload. Returns an error dict or None."""

    if not isinstance(value, dict):
        return {
            "error_code": "invalid_pre_action",
            "error_type": "ValueError",
            "error": "pre_action must be an object with a 'kind' string",
        }
    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return {
            "error_code": "invalid_pre_action",
            "error_type": "ValueError",
            "error": "pre_action requires a string 'kind'",
        }
    return None


def _contract_error_result(
    tool: OperationHandler,
    contract_error: dict[str, Any],
    error_kind: str | None,
) -> ToolResult:
    """Render the ToolResult for a kwargs-contract failure."""

    if error_kind == "unknown_arguments":
        text = format_unknown_args(
            list(contract_error.get("unknown", [])),
            sorted(tool._allowed_top_level_kwargs()),
            dict(contract_error.get("suggested_path") or {}),
        )
    else:
        text = format_error_text(
            "validation_error",
            str(
                contract_error.get("message")
                or contract_error.get("error")
                or "validation failed"
            ),
        )
    return ToolResult(text=text, is_error=True)


def _coercion_error_result(coercion_error: dict[str, Any]) -> ToolResult:
    return ToolResult(
        text=format_error_text(
            str(coercion_error.get("error_code") or "coercion_error"),
            str(coercion_error.get("error") or "input coercion failed"),
        ),
        is_error=True,
    )


def _error_result(err: dict[str, Any]) -> ToolResult:
    code = str(err.get("error_code") or err.get("error_type") or "tool_error")
    message = str(err.get("error") or err.get("message") or "tool error")
    hint_raw = err.get("hint")
    hint = hint_raw if isinstance(hint_raw, str) and hint_raw else None
    text = format_error_text(code, message, hint)
    missing = err.get("missing")
    if isinstance(missing, list) and missing:
        text += f"\nMissing: {', '.join(str(m) for m in missing)}"
    return ToolResult(text=text, is_error=True)


_TASK_DESCRIPTION = (
    "Tagged-union description of what to do when the cron fires. Each "
    "task.kind owns its own pipeline (data gathering + optional LLM "
    "invocation + push to the user's chat). Required fields depend on "
    "`kind`:\n"
    "  - kind='agent_chat_reply' — the agent composes a reply that is "
    "pushed back into the user's chat as an assistant message. Use for "
    "ANY 'remind me / tell me / message me' intent. Required params:\n"
    "      user_request (str): the user's ORIGINAL phrasing of what they "
    "want said/done, verbatim. NEVER pass the literal message to be "
    "delivered — the LLM will compose that from this user_request when "
    "the cron fires. Bad: 'hi'. Good: 'remind me to say hi in 1 minute'.\n"
    "      target_session_id (str, optional): auto-filled to the calling "
    "session; do not specify manually.\n"
    "Scheduling strategy execution is NOT done through cron task_kinds — "
    "the legacy 'strategy_signal_alert' / 'strategy_cycle' kinds are "
    "retired. Use a Task Trigger instead "
    "(doyoutrade-cli task trigger add ...).\n"
    "Hard rule: at fire time the agent's reply will be auto-appended as "
    "an assistant message on target_session_id. The agent must not call "
    "send_message / push / IM tools — delivery is handled by the system."
)


class CreateCronJobTool(OperationHandler):
    name = "create_cron_job"
    description = (
        "Schedule the agent to be re-invoked at a future time. The CANONICAL "
        "tool for any delayed or recurring intent — 'X 秒/分钟/小时后 / "
        "明天 / 后天 / 每天 / 每周 / 每隔 / 定时 / 提醒 / later / every / "
        "at HH:MM'. Never emulate this with `execute_bash sleep` / `at` / "
        "`crontab` — that blocks the current turn and produces no message. "
        "Timing lives in `schedule` (tagged union; see field description): "
        "for one-shot / sub-minute delays use kind='once_at' with "
        "`delay_seconds`; for recurring intervals use kind='every' with "
        "`every_seconds`; only use kind='cron' for calendar-style patterns "
        "'every' can't express. All schedules run in Asia/Shanghai "
        "(UTC+8); sub-minute targets round UP to the next whole minute. "
        "What runs at fire time lives in `task` (tagged union; see field "
        "description). For reminders / message-back-to-user intents use "
        "task.kind='agent_chat_reply' with the user's ORIGINAL phrasing "
        "in params.user_request — do NOT pre-compose the literal message "
        "to deliver, the LLM writes that at fire time. To schedule strategy "
        "execution ('run strategies and tell me the signal'), use a Task "
        "Trigger (doyoutrade-cli task trigger add ...), NOT a cron task_kind. "
        "For a true one-shot, "
        "either the task itself can call delete_cron_job(job_id), or you "
        "can use schedule.kind='once_at' (the job stays in the DB but "
        "won't fire again). By default the job is owned by the calling "
        "agent; pass agent_id only when scheduling for a different agent. "
        "Use pause_cron_job / resume_cron_job to toggle, delete_cron_job "
        "to remove."
    )
    category = "agent"
    requires_calling_agent_id = True
    requires_calling_session_id = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {
                "type": "string",
                "description": (
                    "The agent ID that owns this cron job. Omit to default to "
                    "the calling agent — do not invent placeholder strings."
                ),
            },
            "name": {"type": "string", "description": "Human-readable job name"},
            "schedule": _SCHEDULE_FIELD_SCHEMA,
            "task": {
                "type": "object",
                "additionalProperties": False,
                "description": _TASK_DESCRIPTION,
                "required": ["kind", "params"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["agent_chat_reply"],
                        "description": "Task kind — see field description.",
                    },
                    "params": {
                        "type": "object",
                        "description": "Kind-specific params; see field description.",
                    },
                },
            },
            "target_session_id": {
                "type": "string",
                "description": (
                    "Assistant session id that should receive the cron's "
                    "push. Auto-filled from the calling session — do not "
                    "specify manually."
                ),
            },
            # Legacy fields kept for backward-compatible writes against old
            # skill docs / fixtures. New callers must use ``task`` instead;
            # cron_manager's legacy execution branch handles rows that come
            # through here.
            "input_template": {
                "type": "string",
                "description": (
                    "[LEGACY] Jinja2 template for the user-role message "
                    "sent at fire time. Prefer task.kind='agent_chat_reply' "
                    "for new jobs."
                ),
            },
            "pre_action": {
                "type": ["object", "null"],
                "description": "[LEGACY] " + _PRE_ACTION_DESCRIPTION,
                "properties": {
                    "kind": {"type": "string"},
                    "params": {"type": "object"},
                },
            },
        },
        "required": ["name", "schedule"],
    }

    coercion_rules = (
        SchemaCoercion(field="schedule", declared_type="object"),
        SchemaCoercion(field="task", declared_type="object"),
        SchemaCoercion(field="pre_action", declared_type="object"),
    )

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_create_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_create_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)

        coercion = self._apply_schema_coercion(contract.kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {
                    **base_payload,
                    "error_code": coercion.error.get("error_code"),
                    "error": coercion.error.get("error"),
                },
            )
            return _coercion_error_result(coercion.error)
        kwargs = coercion.kwargs
        if coercion.coerced_fields:
            base_payload["coerced_fields"] = coercion.coerced_fields

        pre_action = kwargs.get("pre_action")
        if pre_action is not None:
            err = _validate_pre_action(pre_action)
            if err is not None:
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)

        # Caller must pick exactly one execution form. ``task`` is the new
        # path (JobTaskRegistry-dispatched); ``input_template`` is the
        # legacy two-stage pipeline kept for backward compatibility.
        task = kwargs.get("task")
        input_template_raw = kwargs.get("input_template")
        has_input_template = (
            isinstance(input_template_raw, str) and input_template_raw.strip()
        )
        if task is not None and has_input_template:
            err = {
                "error_code": "conflicting_execution_form",
                "error_type": "ValueError",
                "error": (
                    "`task` and `input_template` are mutually exclusive; "
                    "prefer `task` for new jobs."
                ),
            }
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {**base_payload, **err},
            )
            return _error_result(err)
        if task is None and not has_input_template:
            err = {
                "error_code": "missing_execution_form",
                "error_type": "ValueError",
                "error": (
                    "either `task` (preferred) or `input_template` "
                    "(legacy) must be supplied"
                ),
                "missing": ["task"],
            }
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {**base_payload, **err},
            )
            return _error_result(err)

        # Validate top-level required scalars.
        missing: list[str] = []
        if not isinstance(kwargs.get("agent_id"), str) or not kwargs["agent_id"].strip():
            missing.append("agent_id")
        if not isinstance(kwargs.get("name"), str) or not kwargs["name"].strip():
            missing.append("name")
        if "schedule" not in kwargs or kwargs.get("schedule") is None:
            missing.append("schedule")
        if missing:
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": f"missing required argument(s): {', '.join(missing)}",
                "missing": missing,
            }
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {**base_payload, **err},
            )
            return _error_result(err)

        # Resolve task kind / params with executor-supplied validation. The
        # dispatcher has already injected ``target_session_id`` at the top
        # level (when the calling session is known); merge it into the
        # nested params so the executor sees a fully-formed payload.
        task_kind: str | None = None
        task_params: dict[str, Any] | None = None
        if task is not None:
            if not isinstance(task, dict):
                err = {
                    "error_code": "invalid_task",
                    "error_type": "ValueError",
                    "error": "task must be an object with `kind` and `params`",
                }
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)
            kind_raw = task.get("kind")
            if not isinstance(kind_raw, str) or not kind_raw.strip():
                err = {
                    "error_code": "invalid_task_kind",
                    "error_type": "ValueError",
                    "error": "task.kind is required",
                    "field": "task.kind",
                }
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)
            task_kind = kind_raw.strip()
            executor = self._mgr.task_registry.get(task_kind)
            if executor is None:
                known = ", ".join(self._mgr.task_registry.known_kinds()) or "<none registered>"
                err = {
                    "error_code": "unknown_task_kind",
                    "error_type": "ValueError",
                    "error": f"unknown task.kind {task_kind!r}; known: {known}",
                    "field": "task.kind",
                }
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)
            raw_params = task.get("params") or {}
            if not isinstance(raw_params, dict):
                err = {
                    "error_code": "invalid_task_params",
                    "error_type": "ValueError",
                    "error": "task.params must be an object",
                    "field": "task.params",
                }
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)
            # Merge dispatcher-injected helpers (target_session_id +
            # agent_id) without clobbering explicit overrides the caller
            # may have supplied inside params.
            task_params = dict(raw_params)
            target_sid = kwargs.get("target_session_id")
            if (
                target_sid
                and not task_params.get("target_session_id")
            ):
                task_params["target_session_id"] = target_sid
            task_params.setdefault("agent_id", kwargs["agent_id"])
            val_err = executor.validate_params(task_params)
            if val_err is not None:
                err = {
                    "error_code": val_err.get("error_code", "invalid_task_params"),
                    "error_type": "ValueError",
                    "error": val_err.get("error") or "invalid task.params",
                }
                field = val_err.get("field")
                if field:
                    err["field"] = field
                await emit_debug_event(
                    "operation_create_cron_job.failed",
                    {**base_payload, **err},
                )
                return _error_result(err)

        try:
            normalized = normalize_schedule(kwargs["schedule"])
        except ScheduleValidationError as exc:
            err = {
                "error_code": exc.code,
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {**base_payload, **err},
            )
            return _error_result(err)

        await emit_debug_event(
            "operation_create_cron_job.request",
            {
                **base_payload,
                "agent_id": kwargs.get("agent_id"),
                "schedule_kind": normalized.source_kind,
                "schedule_cron_expression": normalized.cron_expression,
                "schedule_notes": list(normalized.notes),
                "task_kind": task_kind,
                "pre_kind": (pre_action or {}).get("kind") if pre_action else None,
            },
        )
        try:
            job = await self._mgr.create_job({
                "agent_id": kwargs["agent_id"],
                "name": kwargs["name"],
                "cron_expression": normalized.cron_expression,
                "timezone": CRON_TOOL_TIMEZONE,
                "input_template": input_template_raw if has_input_template else None,
                "pre_action": pre_action,
                "task_kind": task_kind,
                "task_params_json": task_params,
            })
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_create_cron_job.failed",
                {**base_payload, **err},
            )
            return _error_result(err)

        await emit_debug_event(
            "operation_create_cron_job.created",
            {**base_payload, "job_id": job["id"]},
        )
        text = (
            f"Created cron job (job_id={job['id']}) "
            f"for agent {kwargs['agent_id']!r} on schedule "
            f"{normalized.cron_expression!r} (kind={normalized.source_kind})."
        )
        if normalized.notes:
            text += "\nNote: " + "; ".join(normalized.notes)
        return ToolResult(text=text)


class ListCronJobsTool(OperationHandler):
    name = "list_cron_jobs"
    description = (
        "List cron jobs. Defaults to the calling agent's jobs; pass agent_id "
        "only when inspecting another agent."
    )
    category = "agent"
    requires_calling_agent_id = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {
                "type": "string",
                "description": (
                    "The agent ID to list cron jobs for. Omit to default to "
                    "the calling agent."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_list_cron_jobs.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_list_cron_jobs.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        agent_id = kwargs.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "agent_id is required",
            }
            await emit_debug_event(
                "operation_list_cron_jobs.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_list_cron_jobs.request",
            {**base_payload, "agent_id": agent_id},
        )
        try:
            jobs = await self._mgr.list_jobs(agent_id=agent_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_list_cron_jobs.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_list_cron_jobs.ok",
            {**base_payload, "agent_id": agent_id, "count": len(jobs)},
        )
        if not jobs:
            text = f"No cron jobs found for agent {agent_id!r}."
        else:
            lines = [f"Found {len(jobs)} cron job(s) for agent {agent_id!r}:"]
            for job in jobs:
                jid = job.get("id", "?")
                jname = job.get("name", "")
                expr = job.get("cron_expression", "")
                enabled = job.get("enabled", True)
                state = "enabled" if enabled else "disabled"
                lines.append(f"- {jid} [{state}] {jname} ({expr})")
            append_pagination_hint(
                lines,
                tool_name=self.name,
                total=len(jobs),
                shown=len(jobs),
                limit=len(jobs),
                offset=0,
                filters={"agent_id": agent_id},
            )
            text = "\n".join(lines)
        return ToolResult(text=text)


class GetCronJobTool(OperationHandler):
    name = "get_cron_job"
    description = "Get details of a specific cron job."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID"},
        },
        "required": ["job_id"],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_get_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_get_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_get_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_get_cron_job.request",
            {**base_payload, "job_id": job_id},
        )
        try:
            job = await self._mgr.get_job(job_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_get_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        if not job:
            err = {
                "error_code": "not_found",
                "error_type": "ValueError",
                "error": f"Job not found: {job_id}",
            }
            await emit_debug_event(
                "operation_get_cron_job.failed",
                {**base_payload, "job_id": job_id, **err},
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_get_cron_job.ok",
            {**base_payload, "job_id": job_id},
        )
        jname = job.get("name", "") if isinstance(job, dict) else ""
        expr = job.get("cron_expression", "") if isinstance(job, dict) else ""
        enabled = job.get("enabled", True) if isinstance(job, dict) else True
        state = "enabled" if enabled else "disabled"
        text = f"Cron job {job_id} [{state}] {jname} ({expr})."
        text = append_json_payload(text, {"status": "ok", "job": job})
        return ToolResult(text=text)


_UPDATE_CRON_JOB_FIELDS = (
    "agent_id",
    "name",
    "schedule",
    "input_template",
    "pre_action",
    "task",
)


class UpdateCronJobTool(OperationHandler):
    name = "update_cron_job"
    description = (
        "Update an existing cron job. Patch semantics: only fields explicitly "
        "supplied are forwarded. Pass pre_action=null to clear an existing "
        "pre-action; omit it to leave the existing pre-action unchanged. "
        "When supplying `schedule`, follow the same tagged-union shape as "
        "create_cron_job — the server translates it to the underlying cron "
        "expression. Schedules remain in Asia/Shanghai (UTC+8); use "
        "pause_cron_job / resume_cron_job to disable or re-enable a job."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID to update"},
            "agent_id": {"type": "string", "description": "The agent ID that owns this cron job"},
            "name": {"type": "string", "description": "Human-readable job name"},
            "schedule": _SCHEDULE_FIELD_SCHEMA,
            "input_template": {
                "type": "string",
                "description": "[LEGACY] Jinja2 template; prefer `task` for new edits.",
            },
            "pre_action": {
                "type": ["object", "null"],
                "description": "[LEGACY] " + _PRE_ACTION_DESCRIPTION,
                "properties": {
                    "kind": {"type": "string"},
                    "params": {"type": "object"},
                },
            },
            "task": {
                "type": "object",
                "additionalProperties": False,
                "description": _TASK_DESCRIPTION,
                "required": ["kind", "params"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["agent_chat_reply"],
                    },
                    "params": {"type": "object"},
                },
            },
        },
        "required": ["job_id"],
    }

    coercion_rules = (
        SchemaCoercion(field="schedule", declared_type="object"),
        SchemaCoercion(field="pre_action", declared_type="object"),
        SchemaCoercion(field="task", declared_type="object"),
    )

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_update_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_update_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)

        provided_kwargs = contract.kwargs
        coercion = self._apply_schema_coercion(provided_kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                "operation_update_cron_job.failed",
                {
                    **base_payload,
                    "error_code": coercion.error.get("error_code"),
                    "error": coercion.error.get("error"),
                },
            )
            return _coercion_error_result(coercion.error)
        provided_kwargs = coercion.kwargs
        if coercion.coerced_fields:
            base_payload["coerced_fields"] = coercion.coerced_fields

        job_id = provided_kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_update_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)

        # Patch semantics: only include fields the caller explicitly provided.
        # pre_action=None is meaningful (clears the field); other-None defaults
        # would be ambiguous so we ignore them.
        updates: dict[str, Any] = {}
        schedule_normalized: NormalizedSchedule | None = None
        for field in _UPDATE_CRON_JOB_FIELDS:
            if field not in provided_kwargs:
                continue
            value = provided_kwargs[field]
            if field == "pre_action":
                # Explicit clear or set.
                if value is not None:
                    err = _validate_pre_action(value)
                    if err is not None:
                        await emit_debug_event(
                            "operation_update_cron_job.failed",
                            {**base_payload, **err},
                        )
                        return _error_result(err)
                updates["pre_action"] = value
                continue
            if value is None:
                # Skip other None defaults — not an explicit clear.
                continue
            if field == "schedule":
                # Translate the tagged-union into the stored cron_expression so
                # the manager / DB schema doesn't need to know about kinds.
                try:
                    schedule_normalized = normalize_schedule(value)
                except ScheduleValidationError as exc:
                    err = {
                        "error_code": exc.code,
                        "error_type": "ValueError",
                        "error": str(exc),
                    }
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                updates["cron_expression"] = schedule_normalized.cron_expression
                continue
            if field == "task":
                # Validate the new task payload against the same registry
                # used at fire time so a partial / mis-named edit is caught
                # at write time rather than failing the next fire.
                if not isinstance(value, dict):
                    err = {
                        "error_code": "invalid_task",
                        "error_type": "ValueError",
                        "error": "task must be an object",
                    }
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                kind_raw = value.get("kind")
                if not isinstance(kind_raw, str) or not kind_raw.strip():
                    err = {
                        "error_code": "invalid_task_kind",
                        "error_type": "ValueError",
                        "error": "task.kind is required",
                    }
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                kind_str = kind_raw.strip()
                executor = self._mgr.task_registry.get(kind_str)
                if executor is None:
                    known = ", ".join(self._mgr.task_registry.known_kinds()) or "<none>"
                    err = {
                        "error_code": "unknown_task_kind",
                        "error_type": "ValueError",
                        "error": f"unknown task.kind {kind_str!r}; known: {known}",
                    }
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                raw_params = value.get("params") or {}
                if not isinstance(raw_params, dict):
                    err = {
                        "error_code": "invalid_task_params",
                        "error_type": "ValueError",
                        "error": "task.params must be an object",
                    }
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                merged_params = dict(raw_params)
                # update_cron_job is not session-scoped, so don't try to
                # auto-fill target_session_id here — the caller must supply
                # it explicitly when switching to a kind that needs one.
                explicit_agent_id = provided_kwargs.get("agent_id")
                if isinstance(explicit_agent_id, str) and explicit_agent_id.strip():
                    merged_params.setdefault("agent_id", explicit_agent_id.strip())
                val_err = executor.validate_params(merged_params)
                if val_err is not None:
                    err = {
                        "error_code": val_err.get("error_code", "invalid_task_params"),
                        "error_type": "ValueError",
                        "error": val_err.get("error") or "invalid task.params",
                    }
                    f = val_err.get("field")
                    if f:
                        err["field"] = f
                    await emit_debug_event(
                        "operation_update_cron_job.failed",
                        {**base_payload, **err},
                    )
                    return _error_result(err)
                updates["task_kind"] = kind_str
                updates["task_params_json"] = merged_params
                continue
            updates[field] = value

        await emit_debug_event(
            "operation_update_cron_job.request",
            {
                **base_payload,
                "job_id": job_id,
                "update_keys": sorted(updates.keys()),
                "schedule_kind": (
                    schedule_normalized.source_kind if schedule_normalized else None
                ),
                "schedule_notes": (
                    list(schedule_normalized.notes) if schedule_normalized else []
                ),
            },
        )

        try:
            job = await self._mgr.update_job(job_id, updates)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_update_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)

        await emit_debug_event(
            "operation_update_cron_job.updated",
            {**base_payload, "job_id": job_id},
        )
        applied = sorted(updates.keys())
        applied_str = f" (applied: {', '.join(applied)})" if applied else ""
        text = f"Updated cron job {job_id}{applied_str}."
        return ToolResult(text=text)


class DeleteCronJobTool(OperationHandler):
    name = "delete_cron_job"
    description = "Delete a cron job."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID to delete"},
        },
        "required": ["job_id"],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_delete_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_delete_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_delete_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_delete_cron_job.request",
            {**base_payload, "job_id": job_id},
        )
        try:
            await self._mgr.delete_job(job_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_delete_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_delete_cron_job.deleted",
            {**base_payload, "job_id": job_id},
        )
        return ToolResult(text=f"Deleted cron job {job_id}.")


class PauseCronJobTool(OperationHandler):
    name = "pause_cron_job"
    description = "Pause a cron job (stops it from firing on schedule)."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID to pause"},
        },
        "required": ["job_id"],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_pause_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_pause_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_pause_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_pause_cron_job.request",
            {**base_payload, "job_id": job_id},
        )
        try:
            job = await self._mgr.pause_job(job_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_pause_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_pause_cron_job.paused",
            {**base_payload, "job_id": job_id},
        )
        return ToolResult(text=f"Paused cron job {job_id}.")


class ResumeCronJobTool(OperationHandler):
    name = "resume_cron_job"
    description = "Resume a paused cron job."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID to resume"},
        },
        "required": ["job_id"],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_resume_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_resume_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_resume_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_resume_cron_job.request",
            {**base_payload, "job_id": job_id},
        )
        try:
            job = await self._mgr.resume_job(job_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_resume_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_resume_cron_job.resumed",
            {**base_payload, "job_id": job_id},
        )
        return ToolResult(text=f"Resumed cron job {job_id}.")


class TriggerCronJobTool(OperationHandler):
    name = "trigger_cron_job"
    description = "Manually trigger a cron job to run immediately (fire-and-forget)."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID to trigger"},
        },
        "required": ["job_id"],
    }

    def __init__(self, cron_manager: Any):
        self._mgr = cron_manager

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_trigger_cron_job.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_trigger_cron_job.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_trigger_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_trigger_cron_job.request",
            {**base_payload, "job_id": job_id},
        )
        try:
            run_id = await self._mgr.trigger_job(job_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_trigger_cron_job.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_trigger_cron_job.triggered",
            {**base_payload, "job_id": job_id, "cron_job_run_id": run_id},
        )
        return ToolResult(
            text=f"Triggered cron job {job_id} (cron_job_run_id={run_id}).",
        )


class ListCronJobRunsTool(OperationHandler):
    name = "list_cron_job_runs"
    description = "List recent runs of a cron job (most recent first)."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "job_id": {"type": "string", "description": "The cron job ID"},
            "limit": {"type": "integer", "description": "Max number of runs to return", "default": 20},
        },
        "required": ["job_id"],
    }

    def __init__(self, run_repository: Any):
        self._repo = run_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload = {"tool": self.name, "input_keys": sorted(kwargs.keys())}
        if contract.error is not None:
            event = (
                "operation_list_cron_job_runs.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_list_cron_job_runs.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        job_id = kwargs.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "job_id is required",
            }
            await emit_debug_event(
                "operation_list_cron_job_runs.failed", {**base_payload, **err}
            )
            return _error_result(err)
        try:
            limit_raw = kwargs.get("limit", 20)
            limit = min(max(int(limit_raw), 1), 200)
        except (TypeError, ValueError):
            err = {
                "error_code": "invalid_limit",
                "error_type": "ValueError",
                "error": "limit must be an integer",
            }
            await emit_debug_event(
                "operation_list_cron_job_runs.failed",
                {**base_payload, "job_id": job_id, **err},
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_list_cron_job_runs.request",
            {**base_payload, "job_id": job_id, "limit": limit},
        )
        try:
            items = await self._repo.list_for_job(job_id, limit=limit)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_list_cron_job_runs.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_list_cron_job_runs.ok",
            {**base_payload, "job_id": job_id, "count": len(items)},
        )
        if not items:
            text = f"No cron job runs found for job {job_id} (limit={limit})."
        else:
            lines = [
                f"Found {len(items)} run(s) for cron job {job_id} (limit={limit}):"
            ]
            for item in items:
                rid = item.get("id", "?")
                rstatus = item.get("status", "")
                lines.append(f"- {rid} [{rstatus}]")
            # The repo does not surface a total count, so we treat the page as
            # complete (no "N more" hint will be emitted). This keeps the
            # call signature consistent with other list tools.
            append_pagination_hint(
                lines,
                tool_name=self.name,
                total=len(items),
                shown=len(items),
                limit=limit,
                offset=0,
                filters={"job_id": job_id},
            )
            text = "\n".join(lines)
        return ToolResult(text=text)


class GetCronJobRunTool(OperationHandler):
    name = "get_cron_job_run"
    description = "Get one cron job run by run id."
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "run_id": {"type": "string", "description": "The cron_job_runs row id"},
        },
        "required": ["run_id"],
    }

    def __init__(self, run_repository: Any):
        self._repo = run_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            event = (
                "operation_get_cron_job_run.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_get_cron_job_run.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            return _contract_error_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        run_id = kwargs.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            err = {
                "error_code": "missing_required",
                "error_type": "ValueError",
                "error": "run_id is required",
            }
            await emit_debug_event(
                "operation_get_cron_job_run.failed", {**base_payload, **err}
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_get_cron_job_run.request",
            {**base_payload, "run_id": run_id},
        )
        try:
            row = await self._repo.get_run(run_id)
        except ValueError as exc:
            err = {
                "error_code": "validation_error",
                "error_type": "ValueError",
                "error": str(exc),
            }
            await emit_debug_event(
                "operation_get_cron_job_run.failed", {**base_payload, **err}
            )
            return _error_result(err)
        if row is None:
            err = {
                "error_code": "not_found",
                "error_type": "ValueError",
                "error": f"cron job run not found: {run_id}",
            }
            await emit_debug_event(
                "operation_get_cron_job_run.failed",
                {**base_payload, "run_id": run_id, **err},
            )
            return _error_result(err)
        await emit_debug_event(
            "operation_get_cron_job_run.ok",
            {**base_payload, "run_id": run_id},
        )
        rstatus = row.get("status", "") if isinstance(row, dict) else ""
        rjob = row.get("job_id", "") if isinstance(row, dict) else ""
        text = f"Cron job run {run_id} [{rstatus}] for job {rjob}.".rstrip()
        text = append_json_payload(text, {"status": "ok", "run": row})
        return ToolResult(text=text)
