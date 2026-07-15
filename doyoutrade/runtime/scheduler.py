from __future__ import annotations

import inspect
from typing import Dict

from doyoutrade.observability import get_logger, get_tracer


logger = get_logger(__name__)
tracer = get_tracer(__name__)


class RuntimeScheduler:
    def __init__(self, on_task_error=None):
        self.tasks: Dict[str, object] = {}
        self.on_task_error = on_task_error

    def register(self, instance):
        self.tasks[instance.task_id] = instance

    def unregister(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    def start(self, task_id: str):
        self.tasks[task_id].status = "running"

    def pause(self, task_id: str):
        self.tasks[task_id].status = "paused"

    def stop(self, task_id: str):
        self.tasks[task_id].status = "stopped"

    async def tick_once(self):
        executed = 0
        for instance in self.tasks.values():
            if instance.status != "running":
                continue
            try:
                with tracer.start_as_current_span("runtime.instance.tick"):
                    try:
                        logger.info(
                            "task tick started task_id=%s name=%s",
                            instance.task_id,
                            instance.config.name,
                        )
                        result = instance.worker.run_cycle()
                        if inspect.isawaitable(result):
                            await result
                        executed += 1
                        logger.info(
                            "task tick completed task_id=%s name=%s total_executed=%s",
                            instance.task_id,
                            instance.config.name,
                            executed,
                        )
                    except Exception:
                        logger.exception("task tick failed task_id=%s", instance.task_id)
                        raise
            except Exception as exc:  # pragma: no cover - best effort safety branch
                instance.status = "error"
                instance.last_error = str(exc)
                if self.on_task_error is not None:
                    await self.on_task_error(instance.task_id, str(exc))
        return executed
