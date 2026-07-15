from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._bash_semantics import (
    interpret_command_result,
    is_silent_command,
)
from doyoutrade.tools._prose import append_json_payload, format_error_text


# Cap how much of the command we put into debug events. The event lands on a
# span attribute, and span backends typically truncate at 4–8KB per attribute,
# so we keep it well under that. The actual command still goes to the
# subprocess verbatim — this is only about telemetry payload size.
_DEBUG_COMMAND_PREVIEW_MAX = 2048


def _truncate_for_debug(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= _DEBUG_COMMAND_PREVIEW_MAX:
        return text
    return text[:_DEBUG_COMMAND_PREVIEW_MAX] + "…[truncated]"


def _build_foreground_text(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    is_error: bool,
    silent: bool,
) -> str:
    """Build the model-facing text for a finished foreground bash command.

    Design goals (mirrors ClaudeCode's ``mapToolResultToToolResultBlockParam``):

    - No ``Bash command finished (...)`` preamble on the happy path — the
      raw output is the result; a leading prose summary only burns tokens.
    - No ``--- stdout ---`` / ``--- stderr ---`` decorative dividers —
      they collide with real output containing the same text, and the
      model can already tell stderr from stdout when the lines actually
      look like errors.
    - ``Exit code N`` is appended **only** when the command is treated
      as an error per :func:`interpret_command_result`. grep returning 1
      (no matches) is a normal outcome, not an error, and gets no suffix.
    - Empty success → either an explicit ``(no output)`` (for commands
      that produce no stdout by design — ``mkdir``, ``mv``…) or just an
      empty string. We use ``(no output)`` whenever stdout+stderr are
      empty so the model never sees a literally empty tool_result.
    """

    stdout = stdout.rstrip("\n")
    stderr = stderr.rstrip("\n")

    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)

    if timed_out:
        parts.append("Command timed out.")
    elif is_error and exit_code is not None:
        parts.append(f"Exit code {exit_code}")

    if not parts:
        # Same marker either way — ``silent`` only affects the debug event
        # payload (``no_output_expected``), so a future UI can render it
        # differently without changing the model-facing text.
        del silent
        parts.append("(no output)")

    return "\n".join(parts)


@dataclass(frozen=True)
class BashPolicyDecision:
    kind: str
    reason: str


class BashPolicyEngine:
    _DENY_EXACT = (
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
    )
    _ASK_PREFIXES = (
        "rm ",
        "mv ",
        "chmod ",
        "chown ",
        "git reset --hard",
    )

    def decide(self, command: str) -> BashPolicyDecision:
        normalized = " ".join((command or "").strip().split())
        lowered = normalized.lower()
        for denied in self._DENY_EXACT:
            if lowered.startswith(denied):
                return BashPolicyDecision("deny", f"command denied by policy: {denied}")
        for prefix in self._ASK_PREFIXES:
            if lowered.startswith(prefix):
                return BashPolicyDecision("ask", f"command requires approval: {prefix.strip()}")
        return BashPolicyDecision("allow", "command allowed")


def _build_subprocess_env(
    *,
    session_id: str | None,
    agent_id: str | None,
) -> dict[str, str]:
    """Build the environment dict for a spawned subprocess.

    Inherits the parent's env and overlays the session-context variables
    that ``doyoutrade-cli`` (and any other doyoutrade subprocess) will pick
    up to populate ``envelope.meta``. Keeps the assistant session_id and
    debug_session_id aligned because in the chat flow they are the same
    id — strategy-cycle / cron / backtest sub-flows can override
    ``DOYOUTRADE_DEBUG_SESSION_ID`` themselves before re-spawning.

    Empty / missing values are skipped so a missing ``agent_id`` never
    produces ``DOYOUTRADE_AGENT_ID=``.

    Also normalizes CLI discovery: prepends the running interpreter's
    bin directory to ``PATH`` (where pyproject's ``[project.scripts]``
    drops ``doyoutrade-cli``) and exports ``DOYOUTRADE_CLI`` as the
    resolved absolute path. This makes ``execute_bash``-driven
    subprocesses able to call the CLI regardless of whether the parent
    process was started with the venv activated.
    """

    import shutil
    import sys

    from doyoutrade.cli._trace import inject_traceparent_into_env
    from doyoutrade.models.invocation_context import model_invocation_context

    env = os.environ.copy()
    if session_id:
        env["DOYOUTRADE_SESSION_ID"] = session_id
        # Until a strategy-cycle / backtest sub-flow overrides this with
        # its own debug session id, the assistant session_id is the
        # debug-session anchor for events emitted by the CLI's spans.
        env.setdefault("DOYOUTRADE_DEBUG_SESSION_ID", session_id)
    if agent_id:
        env["DOYOUTRADE_AGENT_ID"] = agent_id
    ctx = model_invocation_context.get()
    if isinstance(ctx, dict):
        run_id = ctx.get("run_id")
        if isinstance(run_id, str) and run_id:
            env["DOYOUTRADE_RUN_ID"] = run_id

    # CLI discovery: the console script lives next to the Python
    # interpreter that has doyoutrade installed. Prepend that dir to
    # PATH so an `execute_bash` subprocess invoked from a non-venv
    # parent still finds ``doyoutrade-cli`` by name.
    interpreter_bin = os.path.dirname(sys.executable)
    if interpreter_bin:
        existing_path = env.get("PATH", "")
        path_parts = existing_path.split(os.pathsep) if existing_path else []
        if interpreter_bin not in path_parts:
            env["PATH"] = (
                interpreter_bin + (os.pathsep + existing_path if existing_path else "")
            )

    # Belt-and-suspenders: export the absolute path so scripts can do
    # ``$DOYOUTRADE_CLI ...`` even when PATH manipulation is undesirable.
    cli_path = shutil.which("doyoutrade-cli", path=env.get("PATH"))
    if cli_path:
        env["DOYOUTRADE_CLI"] = cli_path

    # W3C tracecontext (traceparent / tracestate) — lets the CLI
    # subprocess attach its spans to the agent's debug session trace.
    inject_traceparent_into_env(env)
    return env


class BashTaskManager:
    def __init__(self, *, base_dir: str | os.PathLike[str] | None = None) -> None:
        self._base_dir = Path(base_dir or (Path.home() / ".doyoutrade" / "assistant" / "bash-tasks"))
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def start_background(
        self,
        *,
        command: str,
        cwd: str,
        timeout: float,
        session_id: str,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task_id = f"bash-{uuid4().hex[:12]}"
        output_path = self._base_dir / f"{task_id}.log"
        started_at = time.time()

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

        row = {
            "task_id": task_id,
            "session_id": session_id,
            "command": command,
            "cwd": cwd,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "timeout": timeout,
            "exit_code": None,
            "output_path": str(output_path),
            "output_preview": "",
            "_process": process,
            "_pump": None,
            "_waiter": None,
        }
        async with self._lock:
            self._tasks[task_id] = row
        row["_pump"] = asyncio.create_task(self._pump_output(task_id))
        row["_waiter"] = asyncio.create_task(self._wait_for_process(task_id))
        return self._public_row(row)

    async def list_tasks(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            rows = list(self._tasks.values())
        if session_id is not None:
            rows = [row for row in rows if row["session_id"] == session_id]
        return [self._public_row(row) for row in rows]

    async def get_task(self, task_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        async with self._lock:
            row = self._tasks.get(task_id)
        if row is None:
            return None
        if session_id is not None and row["session_id"] != session_id:
            return None
        return self._public_row(row)

    async def stop_task(self, task_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        async with self._lock:
            row = self._tasks.get(task_id)
        if row is None:
            return None
        if session_id is not None and row["session_id"] != session_id:
            return None
        process = row["_process"]
        if process.returncode is None:
            row["status"] = "stopping"
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        return self._public_row(row)

    async def aclose(self) -> None:
        async with self._lock:
            rows = list(self._tasks.values())
        for row in rows:
            await self._terminate_row(row)
        for row in rows:
            waiter = row.get("_waiter")
            if waiter is not None:
                with contextlib.suppress(Exception):
                    await waiter

    async def _pump_output(self, task_id: str) -> None:
        async with self._lock:
            row = self._tasks[task_id]
            process = row["_process"]
            output_path = Path(row["output_path"])
        chunks: list[str] = []
        with output_path.open("w", encoding="utf-8") as handle:
            while True:
                chunk = await process.stdout.readline()
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                handle.write(text)
                handle.flush()
                chunks.append(text)
                joined = "".join(chunks)
                if len(joined) > 4000:
                    joined = joined[-4000:]
                    chunks = [joined]
                row["output_preview"] = joined.strip()

    async def _wait_for_process(self, task_id: str) -> None:
        async with self._lock:
            row = self._tasks[task_id]
            process = row["_process"]
        try:
            await asyncio.wait_for(process.wait(), timeout=row["timeout"])
            exit_code = process.returncode
            row["status"] = "completed" if exit_code == 0 else "failed"
            row["exit_code"] = exit_code
        except asyncio.TimeoutError:
            row["status"] = "failed"
            row["exit_code"] = None
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            row["output_preview"] = (row["output_preview"] + "\nTimed out").strip()
        finally:
            pump = row.get("_pump")
            if pump is not None:
                with contextlib.suppress(Exception):
                    await pump
            row["finished_at"] = time.time()

    async def _terminate_row(self, row: dict[str, Any]) -> None:
        process = row["_process"]
        if process.returncode is None:
            row["status"] = "stopping"
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await process.wait()
        pump = row.get("_pump")
        if pump is not None:
            with contextlib.suppress(Exception):
                await pump

    @staticmethod
    def _public_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "command": row["command"],
            "cwd": row["cwd"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "timeout": row["timeout"],
            "exit_code": row["exit_code"],
            "output_path": row["output_path"],
            "output_preview": row["output_preview"],
        }


async def _run_foreground(
    command: str,
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return {
            "status": "ok",
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "timed_out": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "cwd": cwd,
        }
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "stdout": "",
            "stderr": "Command timed out",
            "exit_code": None,
            "timed_out": True,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "cwd": cwd,
            "error": "Command timed out",
        }
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()


class ExecuteBashTool(OperationHandler):
    name = "execute_bash"
    description = "Execute a bash command locally with policy checks and optional background execution."
    category = "system"
    requires_session_id = True
    # Pull the calling agent's id into kwargs so we can propagate it into
    # the subprocess environment — see ``_build_subprocess_env``. The
    # ``doyoutrade-cli`` subprocess reads ``DOYOUTRADE_AGENT_ID`` to populate
    # its envelope ``meta`` block.
    requires_calling_agent_id = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command to execute."},
            "timeout": {"type": "number", "description": "Timeout in seconds.", "default": 60},
            "cwd": {"type": "string", "description": "Working directory.", "default": "."},
            "background": {"type": "boolean", "description": "Run in background.", "default": False},
            "approval_granted": {
                "type": "boolean",
                "description": "Whether the user already approved a risky command.",
                "default": False,
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        *,
        task_manager: BashTaskManager | None = None,
        policy_engine: BashPolicyEngine | None = None,
    ) -> None:
        self._task_manager = task_manager or BashTaskManager()
        self._policy_engine = policy_engine or BashPolicyEngine()

    async def execute(
        self,
        command: str,
        timeout: float = 60,
        cwd: str = ".",
        background: bool = False,
        approval_granted: bool = False,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> ToolResult:
        session_value = session_id or "default"
        resolved_cwd = str(Path(cwd).resolve())
        decision = self._policy_engine.decide(command)

        base_event_payload: dict[str, Any] = {
            "tool": self.name,
            "command": _truncate_for_debug(command),
            "cwd": resolved_cwd,
            "session_id": session_value,
            "agent_id": agent_id,
            "background": bool(background),
        }

        if decision.kind == "deny":
            await emit_debug_event(
                "operation_execute_bash.rejected",
                {
                    **base_event_payload,
                    "policy": "deny",
                    "reason": decision.reason,
                },
            )
            return ToolResult(
                text=format_error_text("bash_policy_deny", decision.reason),
                is_error=True,
            )
        if decision.kind == "ask" and not approval_granted:
            await emit_debug_event(
                "operation_execute_bash.rejected",
                {
                    **base_event_payload,
                    "policy": "ask",
                    "reason": decision.reason,
                    "approval_granted": False,
                },
            )
            return ToolResult(
                text=format_error_text(
                    "bash_approval_required",
                    decision.reason,
                    "rerun with approval_granted=True after operator review.",
                ),
                is_error=True,
            )

        subprocess_env = _build_subprocess_env(
            session_id=session_id,
            agent_id=agent_id,
        )

        if background:
            task = await self._task_manager.start_background(
                command=command,
                cwd=resolved_cwd,
                timeout=float(timeout),
                session_id=session_value,
                env=subprocess_env,
            )
            tid = task.get("task_id") if isinstance(task, dict) else None
            output_path = task.get("output_path") if isinstance(task, dict) else None
            await emit_debug_event(
                "operation_execute_bash.started",
                {
                    **base_event_payload,
                    "task_id": tid,
                    "output_path": output_path,
                    "timeout": float(timeout),
                },
            )
            # Prose only — the model needs ``task_id`` and ``output_path`` to
            # poll, and that's it. Full structured task row lives on the
            # debug event above; ``manage_bash_tasks(action='get')`` is the
            # follow-up read channel.
            text = (
                f"Bash task started in background (task_id={tid}). "
                f"Poll with manage_bash_tasks(action='get', task_id='{tid}'). "
                f"Output is being written to {output_path}."
            )
            return ToolResult(text=text)

        result = await _run_foreground(
            command,
            cwd=resolved_cwd,
            timeout=float(timeout),
            env=subprocess_env,
        )
        rc = result.get("exit_code") if isinstance(result, dict) else None
        stdout = result.get("stdout") if isinstance(result, dict) else ""
        stderr = result.get("stderr") if isinstance(result, dict) else ""
        timed_out = bool(result.get("timed_out") if isinstance(result, dict) else False)
        duration_ms = result.get("duration_ms") if isinstance(result, dict) else None
        stdout = stdout if isinstance(stdout, str) else ""
        stderr = stderr if isinstance(stderr, str) else ""

        semantic = interpret_command_result(command, rc, timed_out=timed_out)
        silent_expected = is_silent_command(command) and not (stdout or stderr)

        text = _build_foreground_text(
            stdout=stdout,
            stderr=stderr,
            exit_code=rc,
            timed_out=timed_out,
            is_error=semantic.is_error,
            silent=silent_expected,
        )

        event_name = (
            "operation_execute_bash.failed"
            if semantic.is_error
            else "operation_execute_bash.completed"
        )
        await emit_debug_event(
            event_name,
            {
                **base_event_payload,
                "exit_code": rc,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
                "stdout_length": len(stdout),
                "stderr_length": len(stderr),
                "return_code_interpretation": semantic.interpretation,
                "no_output_expected": silent_expected,
            },
        )
        return ToolResult(text=text, is_error=semantic.is_error)

    async def aclose(self) -> None:
        await self._task_manager.aclose()


class ManageBashTasksTool(OperationHandler):
    name = "manage_bash_tasks"
    description = "Create, list, inspect, or stop bash background tasks."
    category = "system"
    requires_session_id = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "stop"],
                "description": "Task action to perform.",
            },
            "task_id": {"type": "string", "description": "Background bash task id."},
        },
        "required": ["action"],
    }

    def __init__(self, *, task_manager: BashTaskManager | None = None) -> None:
        self._task_manager = task_manager or BashTaskManager()

    async def execute(self, action: str, task_id: str | None = None, session_id: str | None = None) -> ToolResult:
        if action == "list":
            rows = await self._task_manager.list_tasks(session_id=session_id)
            count = len(rows) if isinstance(rows, list) else 0
            if count == 0:
                header = "No bash tasks in this session."
            else:
                lines = [f"Found {count} bash task(s):"]
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    lines.append(f"- {r.get('task_id')} [{r.get('status')}] {r.get('command', '')}")
                header = "\n".join(lines)
            return ToolResult(text=append_json_payload(header, {"status": "ok", "items": rows}))
        if not task_id:
            return ToolResult(
                text=format_error_text("missing_task_id", "task_id is required for this action"),
                is_error=True,
            )
        if action == "get":
            row = await self._task_manager.get_task(task_id, session_id=session_id)
            if row is None:
                return ToolResult(
                    text=format_error_text("bash_task_not_found", f"bash task not found: {task_id}"),
                    is_error=True,
                )
            header = f"Bash task {task_id} [{row.get('status') if isinstance(row, dict) else ''}]."
            return ToolResult(text=append_json_payload(header, {"status": "ok", "task": row}))
        if action == "stop":
            row = await self._task_manager.stop_task(task_id, session_id=session_id)
            if row is None:
                return ToolResult(
                    text=format_error_text("bash_task_not_found", f"bash task not found: {task_id}"),
                    is_error=True,
                )
            header = f"Bash task {task_id} stopped (status={row.get('status') if isinstance(row, dict) else ''})."
            return ToolResult(text=append_json_payload(header, {"status": "ok", "task": row}))
        return ToolResult(
            text=format_error_text("unsupported_action", f"unsupported action: {action}"),
            is_error=True,
        )

    async def aclose(self) -> None:
        await self._task_manager.aclose()
