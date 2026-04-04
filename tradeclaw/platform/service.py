from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

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
        instance_repository,
        system_state_repository,
        templates: Optional[Dict[str, AgentTemplate]] = None,
        default_data_provider: str = "auto",
    ):
        self.scheduler = scheduler
        self.worker_factory = worker_factory
        self.instance_repository = instance_repository
        self.system_state_repository = system_state_repository
        self.templates = templates or DEFAULT_TEMPLATES
        self.default_data_provider = (default_data_provider or "auto").strip().lower() or "auto"
        self.instances: Dict[str, AgentInstance] = {}
        self.kill_switch_enabled = False
        existing_error_handler = getattr(self.scheduler, "on_instance_error", None)

        async def persist_instance_error(instance_id: str, error_message: str):
            instance = self.instances.get(instance_id)
            if instance is not None:
                instance.status = "error"
                instance.last_error = error_message
            await self.instance_repository.update_status(instance_id, "error", error_message)
            if existing_error_handler is not None:
                await existing_error_handler(instance_id, error_message)

        self.scheduler.on_instance_error = persist_instance_error

    async def create_instance(
        self,
        name: str,
        template_id: str,
        mode: Optional[str] = None,
        orchestrator_mode: Optional[str] = None,
        description: str = "",
        data_provider: Optional[str] = None,
        watch_symbols: Optional[List[str]] = None,
        execution_strategy: str = "",
        account_id: str = "",
        model_id: str = "",
        settings: Optional[dict] = None,
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
        record = await self.instance_repository.create_instance(
            instance_id=str(uuid.uuid4()),
            name=name,
            template_id=template_id,
            mode=config.mode,
            orchestrator_mode=config.orchestrator_mode,
            description=description,
            data_provider=data_provider,
            status="configured",
            last_error="",
            watch_symbols=list(watch_symbols or []),
            execution_strategy=execution_strategy,
            account_id=account_id,
            model_id=model_id,
            settings=settings,
        )
        instance = AgentInstance(
            instance_id=record.instance_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )

        self.instances[instance.instance_id] = instance
        self.scheduler.register(instance)
        return instance

    async def list_instances(self):
        records = await self.instance_repository.list_instances()
        return [await self.get_instance_status(record.instance_id) for record in records]

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

    async def start_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            raise RuntimeError("kill switch enabled")
        instance = await self._load_or_build_instance(record)
        self.scheduler.start(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "running", "")
        return instance

    async def pause_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = await self._load_or_build_instance(record)
        self.scheduler.pause(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "paused", "")
        return instance

    async def stop_instance(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = await self._load_or_build_instance(record)
        self.scheduler.stop(instance.instance_id)
        await self.instance_repository.update_status(instance.instance_id, "stopped", "")
        return instance

    async def tick_once(self):
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            return 0
        return await self.scheduler.tick_once()

    async def _load_or_build_instance(self, record):
        cached = self.instances.get(record.instance_id)
        if cached is not None:
            cached.status = record.status
            cached.last_error = record.last_error
            return cached

        config = AgentInstanceConfig(
            name=record.name,
            mode=record.mode,
            orchestrator_mode=record.orchestrator_mode,
            template_id=record.template_id,
            description=record.description,
            data_provider=record.data_provider,
        )
        worker = self.worker_factory(config)
        instance = AgentInstance(
            instance_id=record.instance_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )
        self.instances[instance.instance_id] = instance
        self.scheduler.register(instance)
        return instance

    async def restore_instances(self) -> int:
        restored = 0
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        for record in await self.instance_repository.list_instances():
            try:
                instance = await self._load_or_build_instance(record)
            except Exception as exc:
                await self.instance_repository.update_status(record.instance_id, "error", str(exc))
                continue

            if record.status == "running" and not self.kill_switch_enabled:
                try:
                    self.scheduler.start(instance.instance_id)
                    restored += 1
                except Exception as exc:
                    instance.status = "error"
                    instance.last_error = str(exc)
                    await self.instance_repository.update_status(instance.instance_id, "error", str(exc))
        return restored

    async def set_kill_switch(self, enabled: bool):
        await self.system_state_repository.set_kill_switch_enabled(enabled)
        self.kill_switch_enabled = enabled
        if enabled:
            for record in await self.instance_repository.list_instances():
                if record.status != "running":
                    continue
                instance = self.instances.get(record.instance_id)
                if instance is not None and instance.status == "running":
                    self.scheduler.stop(instance.instance_id)
                await self.instance_repository.update_status(record.instance_id, "stopped", "")

    async def get_system_state(self):
        records = await self.instance_repository.list_instances()
        kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        return {
            "kill_switch_enabled": kill_switch_enabled,
            "instance_count": len(records),
            "running_count": len([item for item in records if item.status == "running"]),
        }

    async def get_instance_status(self, identifier: str):
        record = await self.instance_repository.get_instance(identifier)
        instance = self.instances.get(record.instance_id)
        cycles = getattr(instance.worker, "cycles", None) if instance is not None else None
        effective = resolve_effective_provider(record.data_provider, self.default_data_provider)
        return {
            "instance_id": record.instance_id,
            "name": record.name,
            "template_id": record.template_id,
            "mode": record.mode,
            "orchestrator_mode": record.orchestrator_mode,
            "description": record.description,
            "status": record.status,
            "cycles": cycles,
            "last_error": record.last_error,
            "data_provider": record.data_provider,
            "data_provider_effective": effective,
            "watch_symbols": list(record.watch_symbols),
            "execution_strategy": record.execution_strategy,
            "account_id": record.account_id,
            "model_id": record.model_id,
            "settings": record.settings,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    async def aclose(self):
        for instance in self.instances.values():
            close = getattr(instance.worker, "aclose", None)
            if close is not None:
                await close()
