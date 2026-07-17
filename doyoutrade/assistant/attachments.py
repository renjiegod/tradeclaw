"""Structured chat attachments — single source of truth for the upload contract.

Historically the frontend embedded the server's absolute filesystem path into
the chat message text (``[Uploaded file: name, path: /abs/path]``). That leaked
the server's directory layout into the user-visible bubble / persisted content
and let the client hand an arbitrary absolute path to the unsandboxed
``read_file`` tool.

The structured model keeps attachments as data, separate from the user's text:

* ``/upload`` returns an opaque ``file_id`` (the on-disk storage name) plus
  display metadata — never an absolute path.
* The client sends ``attachments`` alongside ``content``; the persisted user
  message stores only the user's own text in ``content`` and the structured
  attachments in ``metadata.attachments`` (no absolute path).
* The absolute path lives only server-side: it is resolved from ``file_id``
  against :data:`UPLOADS_DIR` and injected into the *model-visible* text so the
  agent still knows which path to ``read_file``. The user never sees it.

``file_id`` never becomes a path the caller controls: it must match the exact
shape produced by the upload handler (``uuid4().hex`` + optional extension) and
resolve to a real file inside ``UPLOADS_DIR``, which closes the arbitrary-path
read hole the old text-embedding approach left open.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# uploads/ lives at the repo root:
# doyoutrade/assistant/attachments.py -> assistant -> doyoutrade -> repo/
UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"

# The upload handler names files ``uuid4().hex`` (32 lowercase hex chars) plus
# the original extension (Path.suffix, single leading dot). Anything else is
# not a file_id we minted, so it is rejected before touching the filesystem.
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}(?:\.[A-Za-z0-9]+)?$")


class AttachmentError(ValueError):
    """Structured validation error for the attachment contract.

    Carries a stable ``error_code`` so the API layer can map it to a 400 and
    callers/skills can branch on it instead of parsing free text.
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def resolve_upload_path(file_id: Any) -> Path:
    """Map an opaque ``file_id`` to its absolute path inside :data:`UPLOADS_DIR`.

    Rejects anything that is not a well-formed id, escapes the uploads
    directory, or does not point at an existing file. Raises
    :class:`AttachmentError` (never returns a path the caller could steer
    outside ``uploads/``).
    """

    if not isinstance(file_id, str) or not _FILE_ID_RE.match(file_id):
        raise AttachmentError(
            f"invalid attachment file_id: {file_id!r}",
            error_code="invalid_attachment_file_id",
        )
    uploads_root = UPLOADS_DIR.resolve()
    candidate = (uploads_root / file_id).resolve()
    # Defense in depth: even though the regex forbids separators/'..', confirm
    # the resolved path still sits directly under uploads/.
    if candidate.parent != uploads_root:
        raise AttachmentError(
            f"attachment path escapes uploads dir: {file_id!r}",
            error_code="invalid_attachment_file_id",
        )
    if not candidate.is_file():
        raise AttachmentError(
            f"attachment not found: {file_id!r}",
            error_code="attachment_not_found",
        )
    return candidate


def normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    """Validate & normalize the client-supplied ``attachments`` array.

    Each item must carry a ``file_id`` (resolving to a real uploaded file) and a
    non-empty ``filename``; ``mime_type`` / ``size_bytes`` are optional. Returns
    a clean list of plain dicts safe to persist in message metadata. Raises
    :class:`AttachmentError` on any malformed entry so the API boundary can
    reject the request with a 400 rather than persisting garbage.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("attachments must be a list", error_code="invalid_attachments")
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AttachmentError(
                f"attachments[{idx}] must be an object",
                error_code="invalid_attachments",
            )
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise AttachmentError(
                f"attachments[{idx}].filename is required",
                error_code="invalid_attachments",
            )
        file_id = item.get("file_id")
        # Validates format + existence; raises AttachmentError on failure.
        resolve_upload_path(file_id)
        entry: dict[str, Any] = {"file_id": file_id, "filename": filename.strip()}
        mime = item.get("mime_type")
        if isinstance(mime, str) and mime.strip():
            entry["mime_type"] = mime.strip()
        size = item.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            entry["size_bytes"] = size
        normalized.append(entry)
    return normalized


def render_attachments_for_model(attachments: list[dict[str, Any]] | None) -> str:
    """Render structured attachments into the model-visible text block.

    Produces one ``[Uploaded file: <name>, path: <abs>]`` line per attachment so
    the agent knows which absolute path to feed ``read_file``. Resolved from
    ``file_id`` at render time — the absolute path is never persisted. This text
    is injected into the model input only; it is never shown to the user.

    A file that vanished after upload degrades to a visible ``(unavailable)``
    marker instead of a path, so the agent doesn't hallucinate a read target.
    """

    if not attachments:
        return ""
    lines: list[str] = []
    for att in attachments:
        filename = str(att.get("filename") or "file")
        try:
            path = resolve_upload_path(att.get("file_id"))
        except AttachmentError:
            lines.append(f"[Uploaded file: {filename} (unavailable)]")
            continue
        lines.append(f"[Uploaded file: {filename}, path: {path}]")
    return "\n".join(lines)


def compose_model_user_text(user_text: str, attachments: list[dict[str, Any]] | None) -> str:
    """Combine the user's own text with the model-visible attachment block.

    This is the single definition of how a user turn looks to the model when it
    carries attachments. It MUST be used both for the live turn and when
    replaying persisted history, so the reconstructed last turn matches the
    live ``fallback_user_text`` exactly (otherwise history replay would append a
    duplicate final user message).
    """

    rendered = render_attachments_for_model(attachments)
    user_text = user_text or ""
    if rendered and user_text:
        return f"{rendered}\n\n{user_text}"
    return rendered or user_text
