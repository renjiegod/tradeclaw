"""
Web UI 静态资源入口
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


UI_DIST_ENV = "QMT_PROXY_UI_DIST_DIR"
DEFAULT_UI_DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"


def get_web_ui_dist_dir() -> Path:
    """解析前端构建产物目录。"""
    override = os.getenv(UI_DIST_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_UI_DIST_DIR.resolve()


def _resolve_ui_asset(dist_dir: Path, asset_path: str) -> Path | None:
    candidate = (dist_dir / asset_path).resolve()
    if candidate != dist_dir and dist_dir not in candidate.parents:
        return None
    return candidate


def serve_web_ui(asset_path: str = "") -> FileResponse:
    """返回 Web UI 入口页或静态资源。"""
    dist_dir = get_web_ui_dist_dir()
    index_file = dist_dir / "index.html"

    if not index_file.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "message": (
                    "Web UI build artifacts not found. "
                    "Run `npm install --prefix web && npm run build --prefix web`."
                )
            },
        )

    clean_path = asset_path.strip("/")
    if not clean_path:
        return FileResponse(index_file)

    candidate = _resolve_ui_asset(dist_dir, clean_path)
    if candidate is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"UI asset not found: {clean_path}"},
        )

    if candidate and candidate.is_file():
        return FileResponse(candidate)

    if Path(clean_path).suffix:
        raise HTTPException(
            status_code=404,
            detail={"message": f"UI asset not found: {clean_path}"},
        )

    return FileResponse(index_file)


def register_web_ui_routes(app: FastAPI) -> None:
    """注册 /ui 静态资源路由。"""

    @app.get("/ui", include_in_schema=False)
    async def web_ui_entry():
        return serve_web_ui()

    @app.get("/ui/", include_in_schema=False)
    async def web_ui_entry_slash():
        return serve_web_ui()

    @app.get("/ui/{asset_path:path}", include_in_schema=False)
    async def web_ui_asset(asset_path: str):
        return serve_web_ui(asset_path)
