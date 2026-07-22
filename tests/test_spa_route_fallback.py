"""SPA 路由与 API 路由撞名时的内容协商（doyoutrade/api/server.py::_mount_frontend）。

历史缺陷：浏览器硬导航 GET /tasks（Accept 含 text/html）命中同名 JSON API 路由，
渲染出裸 JSON 而不是前端应用。修复后：text/html 导航落 index.html，程序化客户端
（fetch/httpx 默认 */*、EventSource text/event-stream）不受影响。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doyoutrade.api import server as api_server

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
INDEX_HTML = "<!doctype html><title>spa-index</title>"


def _build_app(dist_dir: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/tasks")
    def list_tasks() -> dict:
        return {"tasks": []}

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        return {"task_id": task_id}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    with mock.patch.object(api_server, "_frontend_dist_dir", return_value=dist_dir):
        api_server._mount_frontend(app)
    return app


class SpaRouteFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self._tmp.name)
        (self.dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (self.dist / "assets").mkdir()
        (self.dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        self.app = _build_app(self.dist)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_browser_navigation_to_colliding_route_serves_spa(self) -> None:
        with TestClient(self.app) as client:
            resp = client.get("/tasks", headers={"accept": BROWSER_ACCEPT})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("spa-index", resp.text)

    def test_browser_navigation_to_parametrized_route_serves_spa(self) -> None:
        with TestClient(self.app) as client:
            resp = client.get("/tasks/task-123", headers={"accept": BROWSER_ACCEPT})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spa-index", resp.text)

    def test_programmatic_clients_still_reach_api(self) -> None:
        with TestClient(self.app) as client:
            for accept in ("*/*", "application/json"):
                resp = client.get("/tasks", headers={"accept": accept})
                self.assertEqual(resp.status_code, 200, accept)
                self.assertEqual(resp.json(), {"tasks": []}, accept)
            resp = client.get("/tasks/task-9", headers={"accept": "*/*"})
            self.assertEqual(resp.json(), {"task_id": "task-9"})

    def test_non_spa_api_path_not_intercepted_even_for_browser(self) -> None:
        with TestClient(self.app) as client:
            resp = client.get("/health", headers={"accept": BROWSER_ACCEPT})
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_static_assets_served_normally(self) -> None:
        with TestClient(self.app) as client:
            resp = client.get("/assets/app.js", headers={"accept": "*/*"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("console.log", resp.text)

    def test_spa_prefixes_cover_known_colliding_api_routes(self) -> None:
        # 已知与前端一级路由撞名的后端 GET 路由；新增撞名路由时应能被该集合覆盖。
        for prefix in ("tasks", "accounts", "watchlist", "approvals"):
            self.assertIn(prefix, api_server.SPA_ROUTE_PREFIXES)


if __name__ == "__main__":
    unittest.main()
