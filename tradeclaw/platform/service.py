from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from tradeclaw.data.factory import resolve_effective_provider
from tradeclaw.runtime.instance import AgentInstance, AgentInstanceConfig


@dataclass(frozen=True)
class AgentTemplate:
    template_id: str
    name: str
    default_mode: str
    default_orchestrator_mode: str


DEFAULT_TEMPLATES: Dict[str, AgentTemplate] = {
    "single-agent-trend": AgentTemplate(
        template_id="single-agent-trend",
        name="Single Agent / Trend Following",
        default_mode="paper",
        default_orchestrator_mode="single-agent",
    ),
    "single-agent-event": AgentTemplate(
        template_id="single-agent-event",
        name="Single Agent / Event Driven",
        default_mode="paper",
        default_orchestrator_mode="single-agent",
    ),
    "multi-role-rtr": AgentTemplate(
        template_id="multi-role-rtr",
        name="Multi Role / Research + Trader + Risk",
        default_mode="paper",
        default_orchestrator_mode="multi-role",
    ),
}


class TradingPlatformService:
    def __init__(
        self,
        scheduler,
        worker_factory: Callable[[AgentInstanceConfig], object],
        templates: Optional[Dict[str, AgentTemplate]] = None,
        default_data_provider: str = "auto",
    ):
        self.scheduler = scheduler
        self.worker_factory = worker_factory
        self.templates = templates or DEFAULT_TEMPLATES
        self.default_data_provider = (default_data_provider or "auto").strip().lower() or "auto"
        self.instances: Dict[str, AgentInstance] = {}
        self.kill_switch_enabled = False

    def create_instance(
        self,
        name: str,
        template_id: str,
        mode: Optional[str] = None,
        orchestrator_mode: Optional[str] = None,
        description: str = "",
        data_provider: Optional[str] = None,
    ) -> AgentInstance:
        template = self.templates.get(template_id)
        if template is None:
            raise KeyError(f"unknown template_id: {template_id}")

        config = AgentInstanceConfig(
            name=name,
            mode=mode or template.default_mode,
            orchestrator_mode=orchestrator_mode or template.default_orchestrator_mode,
            template_id=template_id,
            description=description,
            data_provider=data_provider,
        )
        worker = self.worker_factory(config)
        instance = AgentInstance(config=config, worker=worker)

        self.instances[instance.instance_id] = instance
        self.scheduler.register(instance)
        return instance

    def list_instances(self):
        return list(self.instances.values())

    def list_templates(self):
        return [
            {
                "template_id": template.template_id,
                "name": template.name,
                "default_mode": template.default_mode,
                "default_orchestrator_mode": template.default_orchestrator_mode,
            }
            for template in self.templates.values()
        ]

    def resolve_instance_id(self, identifier: str) -> str:
        if identifier in self.instances:
            return identifier
        for instance in self.instances.values():
            if instance.config.name == identifier:
                return instance.instance_id
        raise KeyError(f"instance not found: {identifier}")

    def start_instance(self, identifier: str):
        if self.kill_switch_enabled:
            raise RuntimeError("kill switch enabled")
        instance_id = self.resolve_instance_id(identifier)
        self.scheduler.start(instance_id)
        return self.instances[instance_id]

    def pause_instance(self, identifier: str):
        instance_id = self.resolve_instance_id(identifier)
        self.scheduler.pause(instance_id)
        return self.instances[instance_id]

    def stop_instance(self, identifier: str):
        instance_id = self.resolve_instance_id(identifier)
        self.scheduler.stop(instance_id)
        return self.instances[instance_id]

    async def tick_once(self):
        if self.kill_switch_enabled:
            return 0
        return await self.scheduler.tick_once()

    def set_kill_switch(self, enabled: bool):
        self.kill_switch_enabled = enabled
        if enabled:
            for instance in self.instances.values():
                if instance.status == "running":
                    self.scheduler.stop(instance.instance_id)

    def get_system_state(self):
        return {
            "kill_switch_enabled": self.kill_switch_enabled,
            "instance_count": len(self.instances),
            "running_count": len([item for item in self.instances.values() if item.status == "running"]),
        }

    def get_instance_status(self, identifier: str):
        instance_id = self.resolve_instance_id(identifier)
        instance = self.instances[instance_id]
        cycles = getattr(instance.worker, "cycles", None)
        effective = resolve_effective_provider(instance.config.data_provider, self.default_data_provider)
        return {
            "instance_id": instance.instance_id,
            "name": instance.config.name,
            "mode": instance.config.mode,
            "status": instance.status,
            "cycles": cycles,
            "last_error": instance.last_error,
            "data_provider": instance.config.data_provider,
            "data_provider_effective": effective,
        }

    async def aclose(self):
        for instance in self.instances.values():
            close = getattr(instance.worker, "aclose", None)
            if close is not None:
                await close()
