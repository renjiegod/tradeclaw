from __future__ import annotations

from typing import Any

from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text


class ListModelRoutesTool(OperationHandler):
    name = "list_model_routes"
    description = (
        "List all available model routes. "
        "Each route has a route_name that must be passed as settings.model_route_name "
        "when creating a task with create_task."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            repo = self._svc.model_route_repository
            if repo is None:
                return ToolResult(
                    text=format_error_text(
                        "model_route_repository_unavailable",
                        "model_route_repository not available",
                    ),
                    is_error=True,
                )
            routes = await repo.list_routes()
        except Exception as exc:
            return ToolResult(
                text=format_error_text("list_model_routes_failed", str(exc)),
                is_error=True,
            )

        items = [
            {
                "id": r.id,
                "route_name": r.route_name,
                "provider_kind": r.provider_kind,
                "target_model": r.target_model,
                "settings": r.settings,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in routes
        ]
        if not items:
            header = "No model routes configured."
        else:
            lines = [f"Found {len(items)} model route(s):"]
            for r in items:
                lines.append(
                    f"- {r['route_name']} -> {r['provider_kind']}/{r['target_model']} (id={r['id']})"
                )
            header = "\n".join(lines)
        return ToolResult(text=append_json_payload(header, {"status": "ok", "routes": items}))
