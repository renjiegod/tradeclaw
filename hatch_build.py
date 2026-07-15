"""Custom hatchling build hook (runs for the ``wheel`` target only, not for
editable installs — so ``uv run`` / ``uv sync`` in a dev checkout is unaffected).

Bundles the built frontend (``frontend/dist``) into the wheel at
``doyoutrade/_frontend`` so a single ``doyoutrade`` process serves the web UI
same-origin. Because ``frontend/dist`` is a git-ignored build artifact, a fresh
``uvx --from git+...`` clone won't have it — so when it is missing this hook
best-effort runs ``npm ci && npm run build`` if ``npm`` is available. If the
frontend can be neither found nor built (e.g. no Node on the build machine), the
wheel is still produced without the UI and the server degrades to API-only.

Set ``DOYOUTRADE_SKIP_FRONTEND_BUILD=1`` to force API-only and skip the npm step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        self._include_frontend(root, build_data)
        self._include_qmt_proxy(root, build_data)

    def _include_frontend(self, root: Path, build_data: dict) -> None:
        dist = root / "frontend" / "dist"

        if not (dist / "index.html").is_file():
            self._try_build_frontend(root, dist)

        if not (dist / "index.html").is_file():
            self.app.display_warning(
                "frontend/dist not found and could not be built — packaging an "
                "API-only wheel (the server runs without the bundled web UI). "
                "Install Node.js and rebuild, or run `npm --prefix frontend run build` "
                "before `uv build`, to include the UI."
            )
            return

        build_data.setdefault("force_include", {})[str(dist)] = "doyoutrade/_frontend"

    def _include_qmt_proxy(self, root: Path, build_data: dict) -> None:
        """Bundle the qmt-proxy server into the wheel at ``doyoutrade/_qmt_proxy``.

        Enables ``doyoutrade --mode both`` / ``--mode qmt-proxy`` to run the proxy
        in-process (see ``doyoutrade.infra.qmt_proxy_server._qmt_proxy_root``).
        ``app`` + ``generated`` are committed source (required for REST + gRPC);
        ``web/dist`` is the optional debug UI. A missing bundle degrades to a
        wheel where only doyoutrade-only mode works — never fails the build."""

        proxy = root / "qmt-proxy"
        if not (proxy / "app" / "main.py").is_file():
            self.app.display_warning(
                "qmt-proxy/app not found — packaging a wheel where only "
                "`doyoutrade --mode doyoutrade` works (no bundled qmt-proxy)."
            )
            return

        force_include = build_data.setdefault("force_include", {})
        force_include[str(proxy / "app")] = "doyoutrade/_qmt_proxy/app"
        # 打进 wheel 的 qmt-proxy 代码含 MIT 派生部分，许可证文件必须随副本分发
        # （MIT: "shall be included in all copies or substantial portions"）。
        for legal in ("LICENSE", "NOTICE", "licenses"):
            path = proxy / legal
            if not path.exists():
                raise FileNotFoundError(
                    f"qmt-proxy/{legal} is required to distribute the bundled "
                    f"qmt-proxy code but was not found at {path}"
                )
            force_include[str(path)] = f"doyoutrade/_qmt_proxy/{legal}"
        generated = proxy / "generated"
        if generated.is_dir():
            force_include[str(generated)] = "doyoutrade/_qmt_proxy/generated"
        web_dist = proxy / "web" / "dist"
        if (web_dist / "index.html").is_file():
            force_include[str(web_dist)] = "doyoutrade/_qmt_proxy/web/dist"

    def _try_build_frontend(self, root: Path, dist: Path) -> None:
        if os.environ.get("DOYOUTRADE_SKIP_FRONTEND_BUILD") == "1":
            return
        frontend = root / "frontend"
        if not (frontend / "package.json").is_file():
            return
        npm = shutil.which("npm")
        if npm is None:
            return
        install = ["ci"] if (frontend / "package-lock.json").is_file() else ["install"]
        self.app.display_info("building frontend bundle (npm install + build)…")
        try:
            subprocess.run([npm, *install], cwd=frontend, check=True)
            subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            # Never fail the wheel build over the optional UI — degrade to API-only.
            self.app.display_warning(f"frontend build failed ({exc}); packaging API-only wheel")
