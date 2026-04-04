from __future__ import annotations

import inspect
from typing import Dict

from tradeclaw.observability import get_logger, get_tracer


logger = get_logger(__name__)
tracer = get_tracer(__name__)


class RuntimeScheduler:
    def __init__(self, on_instance_error=None):
        self.instances: Dict[str, object] = {}
        self.on_instance_error = on_instance_error

    def register(self, instance):
        self.instances[instance.instance_id] = instance

    def start(self, instance_id: str):
        self.instances[instance_id].status = "running"

    def pause(self, instance_id: str):
        self.instances[instance_id].status = "paused"

    def stop(self, instance_id: str):
        self.instances[instance_id].status = "stopped"

    async def tick_once(self):
        executed = 0
        for instance in self.instances.values():
            if instance.status != "running":
                continue
            try:
                with tracer.start_as_current_span("runtime.instance.tick"):
                    try:
                        logger.info(
                            "instance tick started instance_id=%s name=%s",
                            instance.instance_id,
                            instance.config.name,
                        )
                        result = instance.worker.run_cycle()
                        if inspect.isawaitable(result):
                            await result
                        executed += 1
                        logger.info(
                            "instance tick completed instance_id=%s total_executed=%s",
                            instance.instance_id,
                            executed,
                        )
                    except Exception:
                        logger.exception("instance tick failed instance_id=%s", instance.instance_id)
                        raise
            except Exception as exc:  # pragma: no cover - best effort safety branch
                instance.status = "error"
                instance.last_error = str(exc)
                if self.on_instance_error is not None:
                    await self.on_instance_error(instance.instance_id, str(exc))
        return executed
