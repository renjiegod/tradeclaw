"""Swarm 多智能体 REST API + SSE 路由。

通过 ``build_swarm_router(orchestrator, store)`` 构造，挂载于主 app。SSE 端点
逐字复刻 ``app.py`` 中 ``stream_assistant_events`` 的轮询模式，数据源换成
``SwarmStore.list_events``。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from doyoutrade.swarm.models import RunStatus, SwarmRun
from doyoutrade.swarm.presets import inspect_preset, list_presets
from doyoutrade.swarm.store import SwarmStore


class StartRunBody(BaseModel):
    preset_name: str
    user_vars: dict[str, str] = {}


def _run_to_dict(run: SwarmRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "preset_name": run.preset_name,
        "status": run.status.value,
        "user_vars": run.user_vars,
        "provider": run.provider,
        "model": run.model,
        "final_report": run.final_report,
        "total_input_tokens": run.total_input_tokens,
        "total_output_tokens": run.total_output_tokens,
        "error": run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "tasks": [
            {
                "task_id": t.id,
                "agent_id": t.agent_id,
                "status": t.status.value,
                "depends_on": t.depends_on,
                "summary": t.summary,
                "error": t.error,
                "session_id": t.session_id,
                "started_at": t.started_at,
                "completed_at": t.completed_at,
                "worker_iterations": t.worker_iterations,
            }
            for t in run.tasks
        ],
    }


def build_swarm_router(orchestrator, store: SwarmStore) -> APIRouter:
    """构造 swarm 路由。orchestrator 可为 None（仅暴露 preset 只读端点）。"""
    router = APIRouter(prefix="/swarm", tags=["swarm"])

    @router.get("/presets")
    async def get_presets() -> dict[str, Any]:
        return {"presets": list_presets()}

    @router.get("/presets/{name}")
    async def get_preset(name: str) -> dict[str, Any]:
        try:
            return inspect_preset(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runs", status_code=201)
    async def start_run(body: StartRunBody) -> dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="swarm orchestrator 未启用")
        try:
            run = await orchestrator.start_run(body.preset_name, body.user_vars)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _run_to_dict(run)

    @router.get("/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        runs = await store.list_runs(limit=limit)
        return {"runs": [_run_to_dict(r) for r in runs]}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"swarm run not found: {run_id}")
        return _run_to_dict(run)

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="swarm orchestrator 未启用")
        ok = await orchestrator.cancel_run(run_id)
        return {"cancelled": ok}

    @router.get("/runs/{run_id}/events/stream")
    async def stream_run_events(
        run_id: str,
        last_event_id: str | None = Query(default=None),
    ):
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"swarm run not found: {run_id}")

        async def _events():
            marker = last_event_id
            idle_ticks = 0
            while idle_ticks < 300:
                rows = await store.list_events(run_id, after_id=marker, limit=50)
                if rows:
                    idle_ticks = 0
                    for row in rows:
                        marker = row["event_id"]
                        data = json.dumps(row["payload"], ensure_ascii=False)
                        yield (
                            f"id: {row['event_id']}\n"
                            f"event: {row['event_type']}\n"
                            f"data: {data}\n\n"
                        )
                    # run 终态后再推一帧让客户端收尾，然后结束流
                    fresh = await store.get_run(run_id)
                    if fresh and fresh.status in (
                        RunStatus.completed,
                        RunStatus.failed,
                        RunStatus.cancelled,
                    ):
                        # 仍可能有尾部事件未读，循环下一轮拉完后自然 idle 退出
                        pass
                else:
                    idle_ticks += 1
                    yield ": keep-alive\n\n"
                    fresh = await store.get_run(run_id)
                    if fresh and fresh.status in (
                        RunStatus.completed,
                        RunStatus.failed,
                        RunStatus.cancelled,
                    ):
                        break
                await asyncio.sleep(1)

        return StreamingResponse(_events(), media_type="text/event-stream")

    return router
