from __future__ import annotations

import os

from tradeclaw.api.app import create_app
from tradeclaw.api.runtime_loop import RuntimeTickLoop
from tradeclaw.bootstrap import build_platform_runtime


def build_api_with_runtime(tick_interval_seconds: float | None = None):
    runtime = build_platform_runtime()
    service = runtime["service"]
    approval_gate = runtime["approval_gate"]
    app = create_app(service, approval_gate)

    interval = tick_interval_seconds
    if interval is None:
        interval = float(os.getenv("TRADECLAW_TICK_SECONDS", "5"))

    loop = RuntimeTickLoop(service=service, approval_gate=approval_gate, interval_seconds=interval)
    app.state.runtime_loop = loop

    @app.on_event("startup")
    def _on_startup():
        loop.start()

    @app.on_event("shutdown")
    def _on_shutdown():
        loop.stop()

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("uvicorn is not installed. Install fastapi and uvicorn first.") from exc

    host = os.getenv("TRADECLAW_HOST", "0.0.0.0")
    port = int(os.getenv("TRADECLAW_PORT", "8000"))

    app = build_api_with_runtime()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
