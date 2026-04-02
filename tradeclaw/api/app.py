from __future__ import annotations


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
    def health():
        return {"status": "ok"}

    @app.get("/instances")
    def list_instances():
        return [service.get_instance_status(instance.instance_id) for instance in service.list_instances()]

    @app.get("/templates")
    def list_templates():
        return service.list_templates()

    @app.get("/system/state")
    def get_system_state():
        return service.get_system_state()

    @app.post("/system/kill-switch")
    def set_kill_switch(payload: dict):
        enabled = bool(payload.get("enabled", True))
        service.set_kill_switch(enabled)
        return service.get_system_state()

    @app.post("/system/tick")
    def tick_once():
        executed = service.tick_once()
        expired = approval_gate.expire_pending() if hasattr(approval_gate, "expire_pending") else []
        return {"executed": executed, "expired_count": len(expired)}

    @app.post("/instances")
    def create_instance(payload: dict):
        try:
            instance = service.create_instance(
                name=payload["name"],
                template_id=payload.get("template_id", "single-agent-trend"),
                mode=payload.get("mode"),
                orchestrator_mode=payload.get("orchestrator_mode"),
                description=payload.get("description", ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return service.get_instance_status(instance.instance_id)

    @app.post("/instances/{instance_id}/start")
    def start_instance(instance_id: str):
        try:
            service.start_instance(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return service.get_instance_status(instance_id)

    @app.post("/instances/{instance_id}/pause")
    def pause_instance(instance_id: str):
        try:
            service.pause_instance(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return service.get_instance_status(instance_id)

    @app.post("/instances/{instance_id}/stop")
    def stop_instance(instance_id: str):
        try:
            service.stop_instance(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return service.get_instance_status(instance_id)

    @app.get("/approvals/pending")
    def pending_approvals():
        return [
            {
                "approval_id": item.approval_id,
                "intent_id": item.intent_id,
                "created_at": item.created_at.isoformat(),
                "expires_at": item.expires_at.isoformat(),
            }
            for item in approval_gate.list_pending()
        ]

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str):
        try:
            result = approval_gate.approve(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str):
        try:
            result = approval_gate.reject(approval_id, reason="api reject")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    return app
