"""Custom hatchling build hook (runs for the ``wheel`` target only, not for
editable installs — so ``uv run`` / ``uv sync`` in a dev checkout is unaffected).

Bundles the built frontend (``frontend/dist``) into the wheel at
``doyoutrade/_frontend`` so a single ``doyoutrade`` process serves the web UI
same-origin. Because ``frontend/dist`` is a git-ignored build artifact, a fresh
``uvx --from git+...`` clone won't have it — so when it is missing this hook
best-effort runs ``npm ci && npm run build`` if ``npm`` is available. If the
frontend can be neither found nor built (e.g. no Node on the build machine), the
wheel is still produced without the UI and the server degrades to API-only.

Git provenance (``doyoutrade/_git_version.json``) is frozen *before* the
frontend build so npm/vite side-effects cannot mark a clean tag as dirty.

Set ``DOYOUTRADE_SKIP_FRONTEND_BUILD=1`` to force API-only and skip the npm step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        # Freeze git provenance *before* frontend/npm side-effects. Those steps
        # create ignored (frontend/dist) and occasionally untracked non-ignored
        # paths (e.g. frontend/.vite); recording dirty afterwards falsely marks
        # clean tag installs — including the Windows GUI installer — as dirty.
        self._write_git_version(root, build_data)
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

    def _write_git_version(self, root: Path, build_data: dict) -> None:
        """Freezes ``git describe``/``rev-parse`` into the wheel at
        ``doyoutrade/_git_version.json`` so installed packages (no ``.git``
        checkout) can still report their build provenance via
        ``doyoutrade.version_info`` (used by the ``/version`` API endpoint /
        frontend version badge, so issue reporters can state their exact build).

        ``dirty`` only reflects modifications to *tracked* files. Untracked
        build artifacts must not flip it — otherwise every source build that
        runs ``npm``/``vite`` looks like a dirty tree.
        """
        dirty = self._git_dirty(root)
        tag = self._git(root, ["describe", "--tags", "--always"])
        if dirty and tag and not tag.endswith("-dirty"):
            tag = f"{tag}-dirty"
        info = {
            "tag": tag,
            "commit": self._git(root, ["rev-parse", "HEAD"]),
            "commit_short": self._git(root, ["rev-parse", "--short", "HEAD"]),
            "dirty": dirty,
        }
        if info["commit"] is None:
            self.app.display_warning(
                "could not resolve git commit for this build (not a git checkout, "
                "or git unavailable) — /version will report unknown provenance"
            )

        out_dir = Path(tempfile.mkdtemp(prefix="doyoutrade-git-version-"))
        out_file = out_dir / "_git_version.json"
        out_file.write_text(json.dumps(info))
        build_data.setdefault("force_include", {})[str(out_file)] = "doyoutrade/_git_version.json"

    def _git_dirty(self, root: Path) -> bool:
        """True only when tracked files differ from HEAD (untracked ignored)."""
        # ``--untracked-files=no`` keeps npm/vite caches from marking releases dirty.
        status = self._git(root, ["status", "--porcelain", "--untracked-files=no"])
        return bool(status)

    def _git(self, root: Path, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=5, check=True
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() or None

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
