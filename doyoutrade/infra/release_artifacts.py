"""Prebuilt release wheel URLs shared by installers and the in-app updater.

End-user installs prefer a wheel that already embeds ``doyoutrade/_frontend``
(built in CI with Node). Runtime never needs Node.js — only the Python server
serving that static bundle.
"""

from __future__ import annotations

GITHUB_REPO = "renjiegod/doyoutrade"
GITEE_OWNER = "renjie-god"
GITEE_REPO = "doyoutrade"


def normalize_release_tag(tag: str) -> tuple[str, str]:
    """Return ``(tag_with_v, bare_version)`` for a release tag or version string.

    ``v0.1.10`` / ``0.1.10`` / ``V0.1.10`` all normalize the same way.
    """
    text = str(tag or "").strip()
    if not text:
        raise ValueError("release tag must be non-empty")
    bare = text[1:] if text[:1] in ("v", "V") else text
    if not bare:
        raise ValueError(f"release tag {tag!r} has no version after v-prefix")
    return f"v{bare}", bare


def wheel_filename(version: str) -> str:
    """Hatch/uv default wheel name for this pure-Python package."""
    bare = normalize_release_tag(version)[1]
    return f"doyoutrade-{bare}-py3-none-any.whl"


def github_wheel_url(tag: str) -> str:
    tagged, bare = normalize_release_tag(tag)
    return (
        f"https://github.com/{GITHUB_REPO}/releases/download/"
        f"{tagged}/{wheel_filename(bare)}"
    )


def gitee_wheel_url(tag: str) -> str:
    tagged, bare = normalize_release_tag(tag)
    return (
        f"https://gitee.com/{GITEE_OWNER}/{GITEE_REPO}/releases/download/"
        f"{tagged}/{wheel_filename(bare)}"
    )


def wheel_url_for_mirror(tag: str, mirror: str) -> str:
    """``mirror`` is ``github`` or ``gitee`` (same tokens as install scripts)."""
    side = (mirror or "github").strip().lower()
    if side in ("gitee", "cn", "china"):
        return gitee_wheel_url(tag)
    return github_wheel_url(tag)


def install_requirement_from_wheel(
    *,
    tag: str,
    platform: str,
    mirror: str = "github",
) -> str:
    """PEP 508 direct reference for ``uv tool install --force``.

    Windows keeps the ``qmt-proxy`` extra so ``--force`` does not strip the
    embedded proxy on update (mirrors install.ps1).
    """
    name = "doyoutrade[qmt-proxy]" if platform == "win32" else "doyoutrade"
    return f"{name} @ {wheel_url_for_mirror(tag, mirror)}"
