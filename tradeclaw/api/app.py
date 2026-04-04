from __future__ import annotations

from tradeclaw.observability import get_logger
from tradeclaw.persistence.errors import RecordNotFoundError, StateConflictError


logger = get_logger(__name__)


def _normalize_optional_string(value, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None


def _normalize_required_string(value, *, field_name: str) -> str:
    normalized = _normalize_optional_string(value, field_name=field_name)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_watch_symbols(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("watch_symbols must be a list of strings or a comma-separated string")

    normalized = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("watch_symbols must contain only strings")
        symbol = item.strip()
        if symbol:
            normalized.append(symbol)
    return normalized


def _normalize_settings(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("settings must be an object or null")
    return dict(value)


def create_app(service, approval_gate):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("FastAPI is not installed. Install fastapi and uvicorn.") from exc

    app = FastAPI(title="Tradeclaw API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/instances")
    async def list_instances():
        return await service.list_instances()

    @app.get("/templates")
    async def list_templates():
        return service.list_templates()

    @app.get("/system/state")
    async def get_system_state():
        return await service.get_system_state()

    @app.post("/system/kill-switch")
    async def set_kill_switch(payload: dict):
        enabled = bool(payload.get("enabled", True))
        await service.set_kill_switch(enabled)
        logger.warning("api kill switch updated enabled=%s", enabled)
        return await service.get_system_state()

    @app.post("/system/tick")
    async def tick_once():
        executed = await service.tick_once()
        expired = await approval_gate.expire_pending() if hasattr(approval_gate, "expire_pending") else []
        logger.info(
            "api system tick handled executed=%s expired_count=%s",
            executed,
            len(expired),
        )
        return {"executed": executed, "expired_count": len(expired)}

    @app.post("/instances")
    async def create_instance(payload: dict):
        try:
            instance = await service.create_instance(
                name=_normalize_required_string(payload.get("name"), field_name="name"),
                template_id=_normalize_required_string(
                    payload.get("template_id", "single-agent-trend"),
                    field_name="template_id",
                ),
                mode=_normalize_optional_string(payload.get("mode"), field_name="mode"),
                orchestrator_mode=_normalize_optional_string(
                    payload.get("orchestrator_mode"),
                    field_name="orchestrator_mode",
                ),
                description=_normalize_optional_string(
                    payload.get("description", ""),
                    field_name="description",
                )
                or "",
                data_provider=_normalize_optional_string(
                    payload.get("data_provider"),
                    field_name="data_provider",
                ),
                watch_symbols=_normalize_watch_symbols(payload.get("watch_symbols")),
                execution_strategy=_normalize_optional_string(
                    payload.get("execution_strategy", ""),
                    field_name="execution_strategy",
                )
                or "",
                account_id=_normalize_optional_string(
                    payload.get("account_id", ""),
                    field_name="account_id",
                )
                or "",
                model_id=_normalize_optional_string(
                    payload.get("model_id", ""),
                    field_name="model_id",
                )
                or "",
                settings=_normalize_settings(payload.get("settings")),
            )
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info(
            "api instance created instance_id=%s template_id=%s mode=%s",
            instance.instance_id,
            instance.config.template_id,
            instance.config.mode,
        )
        return await service.get_instance_status(instance.instance_id)

    @app.post("/instances/{instance_id}/start")
    async def start_instance(instance_id: str):
        try:
            await service.start_instance(instance_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api instance started instance_id=%s", instance_id)
        return await service.get_instance_status(instance_id)

    @app.post("/instances/{instance_id}/pause")
    async def pause_instance(instance_id: str):
        try:
            await service.pause_instance(instance_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        logger.info("api instance paused instance_id=%s", instance_id)
        return await service.get_instance_status(instance_id)

    @app.post("/instances/{instance_id}/stop")
    async def stop_instance(instance_id: str):
        try:
            await service.stop_instance(instance_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        logger.info("api instance stopped instance_id=%s", instance_id)
        return await service.get_instance_status(instance_id)

    @app.get("/approvals/pending")
    async def pending_approvals():
        return [
            {
                "approval_id": item.approval_id,
                "intent_id": item.intent_id,
                "created_at": item.created_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
            }
            for item in await approval_gate.list_pending()
        ]

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str):
        try:
            result = await approval_gate.approve(approval_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api approval approved approval_id=%s intent_id=%s", approval_id, result.intent_id)
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str):
        try:
            result = await approval_gate.reject(approval_id, reason="api reject")
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api approval rejected approval_id=%s intent_id=%s", approval_id, result.intent_id)
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    return app
