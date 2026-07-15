from __future__ import annotations

from qmt_proxy_sdk.models.system import AppInfo, HealthStatus, RootInfo, ServiceStatus


class SystemApi:
    def __init__(self, transport) -> None:
        self._transport = transport

    async def get_root(self) -> RootInfo:
        payload = await self._transport.request("GET", "/")
        return RootInfo.model_validate(payload)

    async def get_info(self) -> AppInfo:
        payload = await self._transport.request("GET", "/info")
        return AppInfo.model_validate(payload)

    async def check_health(self) -> HealthStatus:
        payload = await self._transport.request("GET", "/health/")
        return HealthStatus.model_validate(payload)

    async def check_ready(self) -> ServiceStatus:
        payload = await self._transport.request("GET", "/health/ready")
        return ServiceStatus.model_validate(payload)

    async def check_live(self) -> ServiceStatus:
        payload = await self._transport.request("GET", "/health/live")
        return ServiceStatus.model_validate(payload)
