"""SwarmOrchestrator：async DAG 编排引擎。

按拓扑层调度 worker：层内并发（受 Semaphore 限制）、层间串行。每个 worker 直接
复用 doyoutrade 的 ``AssistantService``——建一个临时 agent + 临时 session，跑一次
``send_message`` 到底，把最终回复文本当作该任务的摘要。

重写自 Vibe-Trading ``agent/src/swarm/runtime.py``（线程 + ThreadPoolExecutor →
asyncio + Semaphore；自写 ReAct loop → AssistantService）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from doyoutrade.swarm.dag import topological_layers, validate_dag
from doyoutrade.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmEvent,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
    WorkerStatus,
)
from doyoutrade.swarm.presets import build_run_from_preset
from doyoutrade.swarm.store import SwarmStore

logger = logging.getLogger(__name__)


class _SafeDict(dict):
    """str.format_map 用：缺失键返回空串而非抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


def _safe_format(template: str, values: dict[str, str]) -> str:
    try:
        return template.format_map(_SafeDict(values))
    except (ValueError, IndexError):
        # 模板含非法占位（如裸 {}）时退回原文，不让一个坏模板炸掉整个 run。
        return template


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SwarmOrchestrator:
    """Swarm DAG 编排引擎。

    Attributes:
        _store: SwarmStore 持久化层。
        _assistant_service: 复用的单 agent 执行器（worker 引擎）。
        _agent_repo: agent 仓储（建/删临时 worker agent）。
        _max_workers: 层内最大并发 worker 数。
    """

    def __init__(
        self,
        store: SwarmStore,
        assistant_service,
        agent_repo,
        *,
        max_workers: int = 4,
    ) -> None:
        self._store = store
        self._assistant_service = assistant_service
        self._agent_repo = agent_repo
        self._max_workers = max_workers
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ----------------------------------------------------------------- public
    async def start_run(self, preset_name: str, user_vars: dict[str, str]) -> SwarmRun:
        """启动一个 swarm run。立即返回，执行在后台 task 进行。

        Raises:
            FileNotFoundError: preset 不存在。
            ValueError: DAG 校验失败。
        """
        run = build_run_from_preset(preset_name, user_vars)
        validate_dag(run.tasks)
        run.status = RunStatus.running
        await self._store.create_run(run)

        cancel_event = asyncio.Event()
        self._cancel_events[run.id] = cancel_event
        task = asyncio.create_task(self._execute_run(run, cancel_event))
        self._tasks[run.id] = task
        task.add_done_callback(lambda _t, rid=run.id: self._tasks.pop(rid, None))
        return run

    async def cancel_run(self, run_id: str) -> bool:
        """请求取消一个运行中的 swarm。返回是否成功发出取消信号。"""
        event = self._cancel_events.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    # --------------------------------------------------------------- internal
    async def _emit(
        self,
        run_id: str,
        event_type: str,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        event = SwarmEvent(
            type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            data=data or {},
            timestamp=_now_iso(),
        )
        try:
            await self._store.append_event(run_id, event)
        except Exception:
            logger.warning("swarm: 持久化事件失败 run=%s type=%s", run_id, event_type, exc_info=True)

    async def _execute_run(self, run: SwarmRun, cancel_event: asyncio.Event) -> None:
        """核心编排循环（后台 task）。"""
        run_id = run.id
        await self._emit(run_id, "run_started")

        agent_map: dict[str, SwarmAgentSpec] = {a.id: a for a in run.agents}
        task_map: dict[str, SwarmTask] = {t.id: t for t in run.tasks}
        task_summaries: dict[str, str] = {}
        all_succeeded = True

        try:
            layers = topological_layers(run.tasks)
            for layer_idx, layer_task_ids in enumerate(layers):
                if cancel_event.is_set():
                    await self._cancel_remaining(run, task_map)
                    all_succeeded = False
                    break

                await self._emit(
                    run_id,
                    "layer_started",
                    data={"layer": layer_idx, "tasks": layer_task_ids},
                )

                # 收集本层可派发任务，并对上游未完成的做 DAG gating（标记 blocked）。
                dispatchable: list[str] = []
                for tid in layer_task_ids:
                    task = task_map[tid]
                    blocked = [
                        dep
                        for dep in task.depends_on
                        if task_map[dep].status != TaskStatus.completed
                    ]
                    if blocked:
                        task.status = TaskStatus.blocked
                        task.error = f"上游未完成：{', '.join(blocked)}"
                        task.completed_at = _now_iso()
                        await self._store.update_task(run_id, task)
                        await self._emit(
                            run_id,
                            "task_blocked",
                            agent_id=task.agent_id,
                            task_id=tid,
                            data={"blocked_by": blocked},
                        )
                        all_succeeded = False
                    else:
                        dispatchable.append(tid)

                if not dispatchable:
                    continue

                semaphore = asyncio.Semaphore(self._max_workers)

                async def _guarded(tid: str) -> tuple[str, WorkerResult]:
                    async with semaphore:
                        task = task_map[tid]
                        agent_spec = agent_map.get(task.agent_id)
                        upstream = {
                            key: task_summaries.get(src, "")
                            for key, src in task.input_from.items()
                        }
                        result = await self._run_task_with_retries(
                            run, task, agent_spec, upstream, cancel_event
                        )
                        return tid, result

                results = await asyncio.gather(
                    *(_guarded(tid) for tid in dispatchable)
                )

                for tid, result in results:
                    task = task_map[tid]
                    run.total_input_tokens += result.input_tokens
                    run.total_output_tokens += result.output_tokens
                    task.session_id = result.session_id
                    task.worker_iterations = result.iterations
                    task.completed_at = _now_iso()
                    if result.status == WorkerStatus.completed:
                        task.status = TaskStatus.completed
                        task.summary = result.summary
                        task_summaries[tid] = result.summary
                        await self._store.update_task(run_id, task)
                        await self._emit(
                            run_id,
                            "task_completed",
                            agent_id=task.agent_id,
                            task_id=tid,
                            data={"iterations": result.iterations},
                        )
                    else:
                        all_succeeded = False
                        task.status = TaskStatus.failed
                        task.error = result.error or f"worker 未完成 (status={result.status.value})"
                        await self._store.update_task(run_id, task)
                        await self._emit(
                            run_id,
                            "task_failed",
                            agent_id=task.agent_id,
                            task_id=tid,
                            data={"error": task.error},
                        )

                # 把本层 token 增量落库
                run.completed_at = None
                await self._store.update_run(run)

        except Exception as exc:  # noqa: BLE001
            logger.error("swarm: run %s 异常", run_id, exc_info=True)
            all_succeeded = False
            run.error = str(exc)
            await self._emit(run_id, "run_error", data={"error": str(exc)})

        # 收尾
        if cancel_event.is_set():
            final = RunStatus.cancelled
        elif all_succeeded:
            final = RunStatus.completed
        else:
            final = RunStatus.failed
        run.status = final
        run.completed_at = _now_iso()

        # final_report：取最后一层完成任务的摘要
        try:
            layers = topological_layers(run.tasks)
            for tid in layers[-1] if layers else []:
                if tid in task_summaries:
                    run.final_report = task_summaries[tid]
                    break
        except ValueError:
            pass

        await self._store.update_run(run)
        await self._emit(run_id, "run_completed", data={"status": final.value})
        self._cancel_events.pop(run_id, None)

    async def _run_task_with_retries(
        self,
        run: SwarmRun,
        task: SwarmTask,
        agent_spec: SwarmAgentSpec | None,
        upstream: dict[str, str],
        cancel_event: asyncio.Event,
    ) -> WorkerResult:
        """跑一个 worker，失败自动重试 agent_spec.max_retries 次。"""
        if agent_spec is None:
            return WorkerResult(
                status=WorkerStatus.failed,
                summary="",
                error=f"preset 中找不到 agent '{task.agent_id}'",
            )

        task.status = TaskStatus.in_progress
        task.started_at = _now_iso()
        await self._store.update_task(run.id, task)
        await self._emit(run.id, "task_started", agent_id=agent_spec.id, task_id=task.id)

        in_tok = out_tok = 0
        result: WorkerResult | None = None
        for attempt in range(agent_spec.max_retries + 1):
            if cancel_event.is_set():
                return WorkerResult(status=WorkerStatus.failed, summary="", error="run 已取消")
            if attempt > 0:
                await self._emit(
                    run.id,
                    "task_retry",
                    agent_id=agent_spec.id,
                    task_id=task.id,
                    data={"attempt": attempt + 1, "max_retries": agent_spec.max_retries},
                )
            result = await self._run_one_worker(run, task, agent_spec, upstream)
            in_tok += result.input_tokens
            out_tok += result.output_tokens
            if result.status != WorkerStatus.failed:
                break

        result.input_tokens = in_tok
        result.output_tokens = out_tok
        return result

    async def _run_one_worker(
        self,
        run: SwarmRun,
        task: SwarmTask,
        agent_spec: SwarmAgentSpec,
        upstream: dict[str, str],
    ) -> WorkerResult:
        """单次 worker 执行：临时 agent + 临时 session + 一次 send_message。"""
        # 1) 渲染提示词
        upstream_block = ""
        if upstream:
            parts = [f"### {key}\n{text}" for key, text in upstream.items() if text]
            if parts:
                upstream_block = "## 上游产出\n\n" + "\n\n".join(parts)
        render_vars = {**run.user_vars, "upstream_context": upstream_block}
        system_prompt = _safe_format(agent_spec.system_prompt, render_vars)
        task_prompt = _safe_format(task.prompt_template, render_vars)
        # system_prompt 没有 {upstream_context} 占位的 agent（如第一层多/空头），
        # 把上游块补到消息正文，确保下游 worker 一定看得到上游摘要。
        content = task_prompt
        if upstream_block and "{upstream_context}" not in agent_spec.system_prompt:
            content = f"{upstream_block}\n\n---\n\n{task_prompt}"

        # 2) 临时 agent（status != active，对 agent 列表 UI 不可见）
        agent_id = f"swarm-agent-{run.id}-{agent_spec.id}"
        session_id: str | None = None
        try:
            await self._agent_repo.create_agent(
                {
                    "id": agent_id,
                    "name": f"swarm:{run.id}:{agent_spec.id}",
                    "status": "ephemeral",
                    "system_prompt": system_prompt,
                    "model_route_name": agent_spec.model_route_name or "",
                    "tool_names": list(agent_spec.tools),
                    "skill_names": list(agent_spec.skills),
                    "max_turns": agent_spec.max_iterations,
                }
            )
            # 3) 临时 session
            session = await self._assistant_service.create_session(
                agent_id=agent_id,
                title=f"swarm {run.preset_name} · {agent_spec.id}",
            )
            session_id = session["session_id"]

            # 4) 跑到底（带超时）
            try:
                response = await asyncio.wait_for(
                    self._assistant_service.send_message(
                        session_id=session_id, content=content
                    ),
                    timeout=agent_spec.timeout_seconds,
                )
            except asyncio.TimeoutError:
                return WorkerResult(
                    status=WorkerStatus.timeout,
                    summary="",
                    session_id=session_id,
                    error=f"worker 超时（{agent_spec.timeout_seconds}s）",
                )

            messages = response.get("messages") or []
            summary = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    summary = (msg.get("content") or "").strip()
                    break
            in_tok, out_tok, iterations = _extract_usage(messages)
            if not summary:
                return WorkerResult(
                    status=WorkerStatus.incomplete,
                    summary="",
                    session_id=session_id,
                    iterations=iterations,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    error="worker 未产出文本回复",
                )
            return WorkerResult(
                status=WorkerStatus.completed,
                summary=summary,
                session_id=session_id,
                iterations=iterations,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm: worker 失败 task=%s", task.id, exc_info=True)
            return WorkerResult(
                status=WorkerStatus.failed,
                summary="",
                session_id=session_id,
                error=str(exc),
            )

    async def _cancel_remaining(
        self, run: SwarmRun, task_map: dict[str, SwarmTask]
    ) -> None:
        for task in task_map.values():
            if task.status not in (TaskStatus.completed, TaskStatus.failed):
                task.status = TaskStatus.cancelled
                task.completed_at = _now_iso()
                await self._store.update_task(run.id, task)


def _extract_usage(messages: list[dict]) -> tuple[int, int, int]:
    """从 assistant 消息 metadata 尽力提取 token 用量与迭代数，缺失则 0。"""
    in_tok = out_tok = iters = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        meta = msg.get("metadata") or {}
        trace = meta.get("trace") or {}
        usage = trace.get("usage") or meta.get("usage") or {}
        in_tok += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out_tok += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        tool_calls = meta.get("tool_calls") or []
        iters += len(tool_calls)
    return in_tok, out_tok, iters
