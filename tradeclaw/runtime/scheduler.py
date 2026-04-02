from __future__ import annotations

from typing import Dict


class RuntimeScheduler:
    def __init__(self):
        self.instances: Dict[str, object] = {}

    def register(self, instance):
        self.instances[instance.instance_id] = instance

    def start(self, instance_id: str):
        self.instances[instance_id].status = "running"

    def pause(self, instance_id: str):
        self.instances[instance_id].status = "paused"

    def stop(self, instance_id: str):
        self.instances[instance_id].status = "stopped"

    def tick_once(self):
        executed = 0
        for instance in self.instances.values():
            if instance.status != "running":
                continue
            try:
                instance.worker.run_cycle()
                executed += 1
            except Exception as exc:  # pragma: no cover - best effort safety branch
                instance.status = "error"
                instance.last_error = str(exc)
        return executed
