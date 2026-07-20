#!/usr/bin/env python3
"""Create/update a Gitee Release and upload attach files (wheel, Setup.exe, …).

Used by CI after publishing the same assets to GitHub Releases. Requires
``GITEE_TOKEN`` with release / attach_files scope.

Usage:
  python scripts/sync_gitee_release.py \\
    --tag v0.1.10 \\
    --name "v0.1.10" \\
    --body-file notes.md \\
    --asset dist/doyoutrade-0.1.10-py3-none-any.whl \\
    --asset packaging/windows/dist/DoYouTrade-Setup.exe
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_OWNER = "renjie-god"
DEFAULT_REPO = "doyoutrade"
API = "https://gitee.com/api/v5"


def _die(msg: str, code: int = 1) -> None:
    print(f"sync_gitee_release: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _request(
    method: str,
    path: str,
    *,
    token: str,
    data: dict | None = None,
    files: list[tuple[str, Path]] | None = None,
) -> object:
    url = f"{API}{path}"
    if method == "GET":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={urllib.parse.quote(token)}"
        req = urllib.request.Request(url, method="GET")
    elif files:
        # multipart upload for attach_files
        boundary = "----doyoutradeBoundary7MA4YWxkTrZu0gW"
        body = bytearray()
        fields = {"access_token": token}
        if data:
            fields.update({k: str(v) for k, v in data.items()})
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.extend(f"{value}\r\n".encode())
        for field_name, path in files:
            filename = path.name
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode()
            )
            body.extend(f"Content-Type: {ctype}\r\n\r\n".encode())
            body.extend(path.read_bytes())
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            url,
            data=bytes(body),
            method=method,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    else:
        payload = {"access_token": token}
        if data:
            payload.update(data)
        raw = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw,
            method=method,
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8")
            if not text:
                return None
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _die(f"HTTP {exc.code} {method} {path}: {detail}")
    except urllib.error.URLError as exc:
        _die(f"network error {method} {path}: {exc}")
    return None


def find_release_by_tag(owner: str, repo: str, tag: str, token: str) -> dict | None:
    # Gitee has no direct tag lookup; page through releases.
    page = 1
    while page <= 20:
        payload = _request(
            "GET",
            f"/repos/{owner}/{repo}/releases?page={page}&per_page=50",
            token=token,
        )
        if not isinstance(payload, list) or not payload:
            return None
        for item in payload:
            if isinstance(item, dict) and str(item.get("tag_name") or "") == tag:
                return item
        if len(payload) < 50:
            return None
        page += 1
    return None


def ensure_release(
    owner: str,
    repo: str,
    *,
    tag: str,
    name: str,
    body: str,
    token: str,
) -> dict:
    existing = find_release_by_tag(owner, repo, tag, token)
    if existing:
        print(f"Gitee release {tag} already exists (id={existing.get('id')})")
        return existing
    created = _request(
        "POST",
        f"/repos/{owner}/{repo}/releases",
        token=token,
        data={
            "tag_name": tag,
            "name": name or tag,
            "body": body or tag,
            "target_commitish": "main",
            "prerelease": False,
        },
    )
    if not isinstance(created, dict) or "id" not in created:
        _die(f"unexpected create-release response: {created!r}")
    print(f"Created Gitee release {tag} (id={created['id']})")
    return created


def list_attach_files(owner: str, repo: str, release_id: int, token: str) -> list[dict]:
    payload = _request(
        "GET",
        f"/repos/{owner}/{repo}/releases/{release_id}/attach_files",
        token=token,
    )
    return payload if isinstance(payload, list) else []


def delete_attach(owner: str, repo: str, release_id: int, attach_id: int, token: str) -> None:
    _request(
        "DELETE",
        f"/repos/{owner}/{repo}/releases/{release_id}/attach_files/{attach_id}",
        token=token,
    )


def upload_attach(owner: str, repo: str, release_id: int, path: Path, token: str) -> None:
    print(f"Uploading {path.name} ({path.stat().st_size} bytes)…")
    _request(
        "POST",
        f"/repos/{owner}/{repo}/releases/{release_id}/attach_files",
        token=token,
        files=[("file", path)],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v0.1.10")
    parser.add_argument("--name", default="", help="Release title (defaults to tag)")
    parser.add_argument("--body", default="", help="Release notes text")
    parser.add_argument("--body-file", type=Path, help="Read release notes from file")
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        type=Path,
        help="File to upload (repeatable)",
    )
    args = parser.parse_args(argv)

    token = (os.environ.get("GITEE_TOKEN") or "").strip()
    if not token:
        _die("GITEE_TOKEN is required")

    body = args.body
    if args.body_file:
        body = args.body_file.read_text(encoding="utf-8")

    tag = args.tag.strip()
    if not tag.startswith("v"):
        tag = f"v{tag}"

    release = ensure_release(
        args.owner,
        args.repo,
        tag=tag,
        name=args.name or tag,
        body=body,
        token=token,
    )
    release_id = int(release["id"])

    existing = {str(a.get("name")): a for a in list_attach_files(args.owner, args.repo, release_id, token) if isinstance(a, dict)}
    for asset in args.asset:
        if not asset.is_file():
            _die(f"asset not found: {asset}")
        old = existing.get(asset.name)
        if old and old.get("id") is not None:
            print(f"Replacing existing attach {asset.name} (id={old['id']})")
            delete_attach(args.owner, args.repo, release_id, int(old["id"]), token)
        upload_attach(args.owner, args.repo, release_id, asset, token)

    print("Gitee release sync complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
