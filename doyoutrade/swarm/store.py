"""SwarmStore：基于 SQLAlchemy 的 swarm 运行/任务/事件持久化。

构造时注入与 doyoutrade 其他仓储相同的 ``session_factory``（async_sessionmaker）。
负责 DTO（Pydantic 模型）↔ ORM 行的转换，并提供 SSE 用的 after_id 事件分页
（对标 SqlAlchemyAssistantRepository.list_events）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from doyoutrade.persistence.models import (
    SwarmEventRecord,
    SwarmRunRecord,
    SwarmTaskRecord,
)
from doyoutrade.swarm.models import (
    RunStatus,
    SwarmEvent,
    SwarmRun,
    SwarmTask,
    TaskStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class SwarmStore:
    """swarm 运行的 SQLAlchemy 持久化层。"""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------------ run
    async def create_run(self, run: SwarmRun) -> None:
        """持久化一个新 run 及其全部任务。"""
        async with self.session_factory() as session:
            session.add(
                SwarmRunRecord(
                    id=run.id,
                    preset_name=run.preset_name,
                    status=run.status.value,
                    user_vars=dict(run.user_vars),
                    provider=run.provider,
                    model=run.model,
                    final_report=run.final_report,
                    total_input_tokens=run.total_input_tokens,
                    total_output_tokens=run.total_output_tokens,
                    error=run.error,
                    created_at=_parse_iso(run.created_at) or _utcnow(),
                    completed_at=_parse_iso(run.completed_at),
                )
            )
            for task in run.tasks:
                session.add(self._task_to_record(run.id, task))
            await session.commit()

    async def update_run(self, run: SwarmRun) -> None:
        """更新 run 级字段（状态/报告/token/错误/完成时间）。"""
        async with self.session_factory() as session:
            record = await session.get(SwarmRunRecord, run.id)
            if record is None:
                return
            record.status = run.status.value
            record.final_report = run.final_report
            record.total_input_tokens = run.total_input_tokens
            record.total_output_tokens = run.total_output_tokens
            record.error = run.error
            record.provider = run.provider
            record.model = run.model
            record.completed_at = _parse_iso(run.completed_at)
            await session.commit()

    async def get_run(self, run_id: str) -> SwarmRun | None:
        """加载完整 run（含全部任务），找不到返回 None。"""
        async with self.session_factory() as session:
            record = await session.get(SwarmRunRecord, run_id)
            if record is None:
                return None
            task_rows = (
                await session.execute(
                    select(SwarmTaskRecord)
                    .where(SwarmTaskRecord.run_id == run_id)
                    .order_by(SwarmTaskRecord.id)
                )
            ).scalars().all()
            return self._record_to_run(record, task_rows)

    async def list_runs(self, *, limit: int = 50) -> list[SwarmRun]:
        """按创建时间倒序列出 run（含任务）。"""
        async with self.session_factory() as session:
            run_rows = (
                await session.execute(
                    select(SwarmRunRecord)
                    .order_by(SwarmRunRecord.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            runs: list[SwarmRun] = []
            for record in run_rows:
                task_rows = (
                    await session.execute(
                        select(SwarmTaskRecord)
                        .where(SwarmTaskRecord.run_id == record.id)
                        .order_by(SwarmTaskRecord.id)
                    )
                ).scalars().all()
                runs.append(self._record_to_run(record, task_rows))
            return runs

    # ----------------------------------------------------------------- task
    async def update_task(self, run_id: str, task: SwarmTask) -> None:
        """整体覆盖某任务的状态字段。"""
        async with self.session_factory() as session:
            record = (
                await session.execute(
                    select(SwarmTaskRecord).where(
                        SwarmTaskRecord.run_id == run_id,
                        SwarmTaskRecord.task_id == task.id,
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                return
            record.status = task.status.value
            record.summary = task.summary
            record.error = task.error
            record.session_id = task.session_id
            record.worker_iterations = task.worker_iterations
            record.started_at = _parse_iso(task.started_at)
            record.completed_at = _parse_iso(task.completed_at)
            await session.commit()

    # ---------------------------------------------------------------- event
    async def append_event(self, run_id: str, event: SwarmEvent) -> dict:
        """追加一条 swarm 事件，返回其行字典。"""
        async with self.session_factory() as session:
            record = SwarmEventRecord(
                event_id=f"sevt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                event_type=event.type,
                payload={
                    "type": event.type,
                    "agent_id": event.agent_id,
                    "task_id": event.task_id,
                    "timestamp": event.timestamp,
                    **event.data,
                },
                created_at=_parse_iso(event.timestamp) or _utcnow(),
            )
            session.add(record)
            await session.commit()
            return {
                "event_id": record.event_id,
                "event_type": record.event_type,
                "payload": record.payload,
            }

    async def list_events(
        self, run_id: str, *, after_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """按 after_id（上一个 event_id）分页拉取事件，供 SSE 轮询。"""
        async with self.session_factory() as session:
            stmt = (
                select(SwarmEventRecord)
                .where(SwarmEventRecord.run_id == run_id)
                .order_by(SwarmEventRecord.id)
                .limit(limit)
            )
            if after_id:
                marker = (
                    await session.execute(
                        select(SwarmEventRecord.id).where(
                            SwarmEventRecord.event_id == after_id,
                            SwarmEventRecord.run_id == run_id,
                        )
                    )
                ).scalar_one_or_none()
                if marker is not None:
                    stmt = stmt.where(SwarmEventRecord.id > marker)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "payload": row.payload,
                }
                for row in rows
            ]

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _task_to_record(run_id: str, task: SwarmTask) -> SwarmTaskRecord:
        return SwarmTaskRecord(
            run_id=run_id,
            task_id=task.id,
            agent_id=task.agent_id,
            status=task.status.value,
            depends_on=list(task.depends_on),
            input_from=dict(task.input_from),
            summary=task.summary,
            error=task.error,
            session_id=task.session_id,
            worker_iterations=task.worker_iterations,
            started_at=_parse_iso(task.started_at),
            completed_at=_parse_iso(task.completed_at),
        )

    @staticmethod
    def _record_to_run(
        record: SwarmRunRecord, task_rows: list[SwarmTaskRecord]
    ) -> SwarmRun:
        tasks = [
            SwarmTask(
                id=row.task_id,
                agent_id=row.agent_id,
                prompt_template="",
                depends_on=list(row.depends_on or []),
                input_from=dict(row.input_from or {}),
                status=TaskStatus(row.status),
                summary=row.summary,
                session_id=row.session_id,
                error=row.error,
                started_at=_iso(row.started_at),
                completed_at=_iso(row.completed_at),
                worker_iterations=row.worker_iterations,
            )
            for row in task_rows
        ]
        return SwarmRun(
            id=record.id,
            preset_name=record.preset_name,
            status=RunStatus(record.status),
            user_vars=dict(record.user_vars or {}),
            tasks=tasks,
            created_at=_iso(record.created_at) or "",
            completed_at=_iso(record.completed_at),
            final_report=record.final_report,
            total_input_tokens=record.total_input_tokens,
            total_output_tokens=record.total_output_tokens,
            provider=record.provider,
            model=record.model,
            error=record.error,
        )
