from __future__ import annotations

from typing import Any


class RuntimeControlPlane:
    def __init__(
        self,
        *,
        service: Any,
        assistant_service: Any,
        capability_registry: Any,
        channel_manager: Any = None,
        channel_repository: Any = None,
        cron_manager: Any = None,
        cron_run_repo: Any = None,
        model_invocation_repository: Any = None,
    ) -> None:
        self._service = service
        self._assistant_service = assistant_service
        self._capability_registry = capability_registry
        self._channel_manager = channel_manager
        self._channel_repository = channel_repository
        self._cron_manager = cron_manager
        self._cron_run_repo = cron_run_repo
        self._model_invocation_repository = model_invocation_repository

    async def status(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        health = "ok"

        try:
            get_state = getattr(self._service, "get_system_state", None)
            system_state = await get_state() if get_state is not None else {}
            checks["service"] = {"available": True, "system_state": system_state}
        except Exception as exc:
            health = "degraded"
            checks["service"] = {
                "available": False,
                "error_type": type(exc).__name__,
                "error": str(exc) or f"{type(exc).__name__} (no message)",
            }

        channels = await self._channel_status()
        assistant = self._assistant_status()

        return {
            "health": health,
            "capabilities": {
                "total": len(self._capability_registry.ids()),
                "kinds": self._capability_registry.kinds(),
            },
            "assistant": assistant,
            "channels": channels,
            "cron": {
                "available": self._cron_manager is not None,
                "run_repository_available": self._cron_run_repo is not None,
            },
            "observability": {
                "model_invocations_available": self._model_invocation_repository is not None,
            },
            "checks": checks,
        }

    async def health(self) -> dict[str, str]:
        status = await self.status()
        return {"status": str(status.get("health") or "unknown")}

    def _assistant_status(self) -> dict[str, Any]:
        tools = []
        list_tools = getattr(self._assistant_service, "list_tools", None)
        if list_tools is not None:
            try:
                tools = list_tools()
            except Exception:
                tools = []
        else:
            from doyoutrade.tools import resolve_tool_registry_factory

            tools = resolve_tool_registry_factory()().list_tools()
        return {
            "available": self._assistant_service is not None,
            "tool_count": len(tools),
        }

    async def _channel_status(self) -> dict[str, Any]:
        manager_ids = list(getattr(self._channel_manager, "channel_ids", []) or [])
        repository_count = None
        if self._channel_repository is not None:
            try:
                repository_count = len(await self._channel_repository.list_channels())
            except Exception:
                repository_count = None
        return {
            "manager_available": self._channel_manager is not None,
            "repository_available": self._channel_repository is not None,
            "registered_ids": manager_ids,
            "repository_count": repository_count,
        }
