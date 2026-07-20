"""Release-based self-update service (设置页「自动更新」).

DoYouTrade is distributed as a uv tool installed from a **prebuilt Release
wheel** (``install.sh`` / ``install.ps1`` / GUI Setup.exe). The wheel embeds
``doyoutrade/_frontend`` so end-user machines never need Node.js. This module
implements the update flow behind the ``auto_update`` config section:

* A background loop — enabled by default, hot-toggled via
  ``auto_update.enabled`` (the loop re-reads ``get_config()`` every tick) —
  polls the GitHub *releases* API every ``check_interval_hours`` and compares
  the latest release tag against the installed package version.
* Finding a newer release only **notifies**: the result is surfaced through
  ``GET /update/status`` and shown as a prompt in the web UI. Nothing is
  installed automatically.
* The user explicitly triggers the update (``POST /update/apply``). Because
  the running process executes from the very uv-tool venv the installer must
  replace (Windows would hit file locks on ``python.exe``), the install is
  *staged*: the server shuts down gracefully and the process ``exec``s into a
  shell that runs ``uv tool install --force "<requirement built by
  _install_requirement()>"`` (Release wheel URL for the tag; ``[qmt-proxy]``
  extra kept on Windows; ``DOYOUTRADE_MIRROR=gitee`` selects the Gitee asset)
  and then starts ``doyoutrade`` again with the original argv. If the install
  fails the shell falls back to relaunching the still-intact current version.

Error visibility (CLAUDE.md §错误可见性): every check / apply failure is
recorded as a structured ``last_error`` (``error_code`` + message) on the
status surface, logged at warning-or-higher with exception type + message, and
marked on the ``update.check`` / ``update.apply`` OTel span.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess  # noqa: F401  (list2cmdline used on Windows below)
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from doyoutrade.config import get_config
from doyoutrade.infra.release_artifacts import install_requirement_from_wheel
from doyoutrade.observability import get_logger, get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/{repo}/releases/latest"

#: Loop granularity. Small so toggling ``auto_update.enabled`` in the UI takes
#: effect within a minute; the actual GitHub poll is paced by
#: ``check_interval_hours``.
TICK_SECONDS = 60.0

#: Delay before the first automatic check after startup. Long enough that
#: short-lived processes (tests, e2e runs, CLI-driven boots) never hit the
#: network; a user opening the UI right after boot can always POST
#: /update/check for an immediate check.
INITIAL_DELAY_SECONDS = 300.0

_HTTP_TIMEOUT_SECONDS = 10.0


class UpdateError(Exception):
    """A structured, user-visible update failure.

    ``error_code`` is a stable token (surfaced through the API / UI);
    ``hint`` points at the fix.
    """

    def __init__(self, error_code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


@dataclass(frozen=True)
class ReleaseInfo:
    """The latest GitHub release, as far as the updater cares."""

    version: str  # normalized, e.g. "0.2.0"
    tag: str  # raw tag_name, used as the git ref to install
    name: str | None
    published_at: str | None
    html_url: str | None
    notes: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "name": self.name,
            "published_at": self.published_at,
            "html_url": self.html_url,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class StagedUpdate:
    """An accepted ``apply`` — consumed by :func:`exec_staged_update` after
    the uvicorn server has drained."""

    repo: str
    tag: str
    version: str
    uv_path: str
    argv: tuple[str, ...]


def current_version() -> str:
    """Installed ``doyoutrade`` version from package metadata.

    Works both for the uv-tool install and a dev checkout (editable install
    still writes metadata). A missing distribution is a packaging bug — raise
    rather than pretend a version.
    """
    try:
        return importlib_metadata.version("doyoutrade")
    except importlib_metadata.PackageNotFoundError as exc:  # pragma: no cover
        raise UpdateError(
            "package_metadata_missing",
            "doyoutrade package metadata not found; the installation is broken "
            f"({type(exc).__name__}: {exc})",
            hint="reinstall with install.sh / uv tool install",
        ) from exc


def detect_install_kind() -> str:
    """``source`` for a git checkout (``uv run`` in the repo), ``package`` for
    an installed wheel (uv tool / pip). ``apply`` refuses ``source`` — a dev
    checkout updates via ``git pull``, not ``uv tool install``."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return "source"
    return "package"


def parse_version_tag(tag: str) -> tuple[int, ...] | None:
    """Parse ``v1.2.3`` / ``1.2.3`` into a comparable int tuple.

    Returns None for anything else (pre-release suffixes, date tags, …) —
    the caller must surface that as ``invalid_release_tag``, never guess.
    """
    text = str(tag or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        return None
    parts = text.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


async def _fetch_latest_release_github(repo: str) -> dict[str, Any] | None:
    """GET the latest release JSON from GitHub; None when the repo has no
    releases (404). Any other failure raises :class:`UpdateError`."""
    import httpx

    url = GITHUB_LATEST_RELEASE_URL.format(repo=repo)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "doyoutrade-updater",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UpdateError(
            "github_unreachable",
            f"failed to reach GitHub releases API ({type(exc).__name__}: {exc})",
            hint="check network connectivity / proxy, then retry",
        ) from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise UpdateError(
            "github_api_error",
            f"GitHub releases API returned HTTP {response.status_code} for {repo}: "
            f"{response.text[:200]}",
            hint="verify auto_update.repo and GitHub API rate limits",
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise UpdateError(
            "github_api_error",
            f"GitHub releases API returned non-object JSON for {repo}",
        )
    return payload


class UpdateService:
    """Background release checker + user-triggered update staging.

    Lifecycle mirrors :class:`ObservabilityPruneService`: a single asyncio
    task with ``start()`` / ``stop()`` wired into the API server. All state
    mutation happens on the event loop, so no locking is needed.
    """

    def __init__(
        self,
        *,
        fetch_latest_release: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        install_kind: str | None = None,
        version: str | None = None,
        tick_seconds: float = TICK_SECONDS,
        initial_delay_seconds: float = INITIAL_DELAY_SECONDS,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._fetch_latest_release = fetch_latest_release or _fetch_latest_release_github
        self._install_kind = install_kind or detect_install_kind()
        self._current_version = version or current_version()
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._which = which

        self._task: asyncio.Task[None] | None = None
        self._next_check_monotonic: float | None = None
        self._checking = False
        self._latest: ReleaseInfo | None = None
        self._update_available = False
        self._last_checked_at: datetime | None = None
        self._last_error: dict[str, Any] | None = None
        self._staged: StagedUpdate | None = None
        self._restart_requester: Callable[[], None] | None = None

    # --- wiring -----------------------------------------------------------

    def bind_restart_requester(self, requester: Callable[[], None]) -> None:
        """Install the callback that gracefully stops the uvicorn server.

        Set by ``doyoutrade/api/server.py`` once the ``Server`` object exists;
        without it ``apply`` is refused (``restart_unsupported``)."""
        self._restart_requester = requester

    @property
    def staged_update(self) -> StagedUpdate | None:
        return self._staged

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._next_check_monotonic = time.monotonic() + self._initial_delay_seconds
            self._task = asyncio.create_task(self._loop(), name="update-checker")
            logger.info(
                "UpdateService started version=%s install_kind=%s first_check_in=%.0fs",
                self._current_version,
                self._install_kind,
                self._initial_delay_seconds,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("UpdateService stopped")

    # --- background loop ----------------------------------------------------

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            if self._staged is not None:
                continue  # update accepted; restart imminent — stop polling
            try:
                cfg = get_config().auto_update
            except Exception:
                # Config unreadable is a real fault the operator must see, but
                # the checker must survive to retry once config is fixed.
                logger.exception("auto_update config read failed; retrying next tick")
                continue
            if not cfg.enabled:
                continue
            now = time.monotonic()
            if self._next_check_monotonic is not None and now < self._next_check_monotonic:
                continue
            self._next_check_monotonic = now + max(
                self._tick_seconds, cfg.check_interval_hours * 3600.0
            )
            try:
                await self.check_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                # check_now records + logs structured failures itself; this
                # guard only keeps an unexpected bug from killing the loop.
                logger.exception("update check iteration failed unexpectedly")

    # --- check ---------------------------------------------------------------

    async def check_now(self) -> dict[str, Any]:
        """Poll GitHub for the latest release and refresh the status surface.

        Never raises for *known* failure modes — they land in ``last_error``
        with a stable ``error_code`` so both the API caller and the background
        loop share one visibility path.
        """
        if self._checking:
            return self.status()
        self._checking = True
        repo = get_config().auto_update.repo
        with tracer.start_as_current_span("update.check") as span:
            span.set_attribute("update.repo", repo)
            span.set_attribute("update.current_version", self._current_version)
            try:
                payload = await self._fetch_latest_release(repo)
                self._last_checked_at = datetime.now(timezone.utc)
                if payload is None:
                    # Repo has no releases yet — a normal state, not an error.
                    self._latest = None
                    self._update_available = False
                    self._last_error = None
                    span.set_attribute("update.status", "no_releases")
                    logger.info("update check: %s has no releases yet", repo)
                else:
                    release = self._parse_release(payload, repo=repo)
                    current = parse_version_tag(self._current_version)
                    latest = parse_version_tag(release.tag)
                    if latest is None:
                        raise UpdateError(
                            "invalid_release_tag",
                            f"latest release tag {release.tag!r} of {repo} is not a "
                            "comparable version (expected v<major>.<minor>.<patch>)",
                            hint="tag releases as vX.Y.Z",
                        )
                    if current is None:
                        raise UpdateError(
                            "invalid_current_version",
                            f"installed version {self._current_version!r} is not comparable",
                            hint="reinstall from a release tag",
                        )
                    self._latest = release
                    self._update_available = latest > current
                    self._last_error = None
                    span.set_attribute("update.latest_version", release.version)
                    span.set_attribute("update.available", self._update_available)
                    span.set_attribute("update.status", "ok")
                    logger.info(
                        "update check: current=%s latest=%s update_available=%s repo=%s",
                        self._current_version,
                        release.version,
                        self._update_available,
                        repo,
                    )
            except UpdateError as exc:
                self._record_error(exc, span=span, phase="check")
            except Exception as exc:
                self._record_error(
                    UpdateError(
                        "update_check_failed",
                        f"unexpected failure during update check ({type(exc).__name__}: {exc})",
                    ),
                    span=span,
                    phase="check",
                )
            finally:
                self._checking = False
        return self.status()

    def _parse_release(self, payload: dict[str, Any], *, repo: str) -> ReleaseInfo:
        tag = str(payload.get("tag_name") or "").strip()
        if not tag:
            raise UpdateError(
                "github_api_error",
                f"latest release of {repo} has no tag_name",
            )
        normalized = tag[1:] if tag[:1] in ("v", "V") else tag
        return ReleaseInfo(
            version=normalized,
            tag=tag,
            name=(str(payload["name"]) if payload.get("name") else None),
            published_at=(
                str(payload["published_at"]) if payload.get("published_at") else None
            ),
            html_url=(str(payload["html_url"]) if payload.get("html_url") else None),
            notes=(str(payload["body"]) if payload.get("body") else None),
        )

    def _record_error(self, exc: UpdateError, *, span: Any, phase: str) -> None:
        self._last_error = {
            "error_code": exc.error_code,
            "message": str(exc),
            "hint": exc.hint,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        span.set_attribute("update.status", "error")
        span.set_attribute("update.error_code", exc.error_code)
        logger.warning(
            "update %s failed error_code=%s message=%s", phase, exc.error_code, exc
        )

    # --- apply (user-triggered) ----------------------------------------------

    async def apply(self) -> dict[str, Any]:
        """Stage the update to the latest known release and trigger a graceful
        restart. Raises :class:`UpdateError` on every refusal — the API layer
        maps it to a structured 4xx."""
        with tracer.start_as_current_span("update.apply") as span:
            span.set_attribute("update.current_version", self._current_version)
            try:
                if self._staged is not None:
                    raise UpdateError(
                        "update_already_staged",
                        f"an update to {self._staged.tag} is already in progress",
                        hint="wait for the server to restart",
                    )
                if self._install_kind == "source":
                    raise UpdateError(
                        "dev_checkout_unsupported",
                        "this server runs from a source checkout; in-place update "
                        "via uv tool install is not applicable",
                        hint="git pull && restart the server",
                    )
                latest = self._latest
                if not self._update_available or latest is None:
                    raise UpdateError(
                        "no_update_available",
                        f"no newer release than {self._current_version} is known; "
                        "run a check first",
                        hint="POST /update/check, then retry",
                    )
                uv_path = self._which("uv")
                if not uv_path:
                    raise UpdateError(
                        "uv_not_found",
                        "the `uv` executable was not found on PATH; cannot reinstall",
                        hint="install uv (https://docs.astral.sh/uv/) or update manually",
                    )
                if self._restart_requester is None:
                    raise UpdateError(
                        "restart_unsupported",
                        "this server was not started through the doyoutrade launcher; "
                        "automatic restart is unavailable",
                        hint="update manually with install.sh / uv tool install",
                    )
                repo = get_config().auto_update.repo
                self._staged = StagedUpdate(
                    repo=repo,
                    tag=latest.tag,
                    version=latest.version,
                    uv_path=uv_path,
                    argv=tuple(_relaunch_argv(self._which)),
                )
                span.set_attribute("update.target_tag", latest.tag)
                span.set_attribute("update.status", "staged")
                logger.info(
                    "update apply accepted: %s -> %s (tag=%s repo=%s); scheduling "
                    "graceful restart",
                    self._current_version,
                    latest.version,
                    latest.tag,
                    repo,
                )
                # Let the HTTP response flush before the server starts draining.
                asyncio.get_running_loop().call_later(0.8, self._restart_requester)
                return self.status()
            except UpdateError as exc:
                self._record_error(exc, span=span, phase="apply")
                raise

    # --- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        cfg = get_config().auto_update
        if self._staged is not None:
            state = "restarting"
        elif self._checking:
            state = "checking"
        else:
            state = "idle"
        return {
            "enabled": cfg.enabled,
            "check_interval_hours": cfg.check_interval_hours,
            "repo": cfg.repo,
            "current_version": self._current_version,
            "install_kind": self._install_kind,
            "state": state,
            "update_available": self._update_available,
            "latest": self._latest.as_dict() if self._latest else None,
            "last_checked_at": (
                self._last_checked_at.isoformat() if self._last_checked_at else None
            ),
            "last_error": self._last_error,
            "restart_supported": self._restart_requester is not None,
        }


# --- restart / reinstall handoff ------------------------------------------------


def _relaunch_argv(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """The argv to relaunch doyoutrade with after the reinstall.

    Normally ``sys.argv`` — the uv-tool shim path plus flags. When argv[0]
    is not a resolvable ``doyoutrade`` launcher (unusual embedding), fall back
    to the ``doyoutrade`` found on PATH while keeping the original flags.
    """
    argv = list(sys.argv)
    exe = argv[0] if argv else ""
    base = os.path.basename(exe).lower()
    if base in ("doyoutrade", "doyoutrade.exe") and (
        (os.path.isabs(exe) and os.path.exists(exe)) or which(exe)
    ):
        return argv
    resolved = which("doyoutrade")
    if resolved:
        return [resolved, *argv[1:]]
    return argv or ["doyoutrade"]


def _update_mirror() -> str:
    """Same tokens as install.ps1 / install.sh (``DOYOUTRADE_MIRROR``)."""
    raw = (os.environ.get("DOYOUTRADE_MIRROR") or "").strip().lower()
    if raw in ("gitee", "cn", "china"):
        return "gitee"
    return "github"


def _install_requirement(
    staged: StagedUpdate,
    *,
    platform: str | None = None,
    mirror: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """PEP 508 direct reference passed to ``uv tool install`` for the update.

    Always installs the prebuilt Release wheel for ``staged.tag`` so the web UI
    stays bundled (no Node / no source build on the client). Windows keeps the
    ``qmt-proxy`` extra so ``--force`` does not strip the embedded proxy.
    ``which`` is accepted for call-site compatibility with older tests but is
    unused — wheels do not need git.
    """
    del which  # wheels replace the former git+/archive branch
    plat = platform if platform is not None else sys.platform
    side = mirror if mirror is not None else _update_mirror()
    return install_requirement_from_wheel(
        tag=staged.tag, platform=plat, mirror=side
    )


def build_restart_command(staged: StagedUpdate) -> list[str]:
    """Shell argv that reinstalls doyoutrade at the staged tag and relaunches.

    Runs AFTER this process exits (via ``os.execv``), so the uv-tool venv is
    no longer locked by a live interpreter (critical on Windows). If the
    install fails, the still-intact current version is relaunched so the
    operator is never left without a server; the uv error stays visible in
    the terminal.
    """
    requirement = _install_requirement(staged)
    relaunch_argv = list(staged.argv) or ["doyoutrade"]
    if sys.platform == "win32":  # pragma: no cover - windows-only branch
        install = subprocess.list2cmdline(
            [staged.uv_path, "tool", "install", "--force", requirement]
        )
        relaunch = subprocess.list2cmdline(relaunch_argv)
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", f"{install} && {relaunch} || {relaunch}"]
    import shlex

    install = shlex.join(
        [staged.uv_path, "tool", "install", "--force", requirement]
    )
    relaunch = shlex.join(relaunch_argv)
    script = f"{install} && exec {relaunch} || exec {relaunch}"
    return ["/bin/sh", "-c", script]


def exec_staged_update(staged: StagedUpdate) -> None:
    """Replace this process with the reinstall-and-relaunch shell.

    Called by ``doyoutrade/api/server.py`` after ``server.serve()`` returns.
    Never returns on success; an exec failure is logged and re-raised so the
    process exits non-zero (visible) instead of silently staying on the old
    version.
    """
    command = build_restart_command(staged)
    logger.info(
        "executing staged update to %s (tag=%s): %s",
        staged.version,
        staged.tag,
        command,
    )
    # Flush stdio so the log line above survives the exec.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(command[0], command)
    except OSError:
        logger.exception(
            "failed to exec staged update command %s; still on version %s",
            command,
            staged.version,
        )
        raise
