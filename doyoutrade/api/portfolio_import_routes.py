"""HTTP surface for broker-statement (交割单) CSV import into the knowledge base.

Three endpoints under ``/portfolio/imports``:

- ``GET  /portfolio/imports/brokers`` — broker directory suggestions: the union
  of directories already present under ``knowledge/trades/`` and a static list
  of common brokers (the same broker families the attribution column-alias
  matrix covers: 华泰 / 国君 / 银河 / 东财 / 中信).
- ``POST /portfolio/imports/csv/parse`` — multipart preview: parses the upload
  and marks each fill duplicate/new against the on-disk monthly files with
  ZERO writes (:func:`doyoutrade.portfolio_import.csv_import.analyze_trades_csv`).
- ``POST /portfolio/imports/csv/commit`` — multipart import (optionally
  ``dry_run``): lands fills into ``trades/<broker>/<YYYY-MM>.csv`` and returns
  the import result including the post-import ``review`` block
  (:func:`doyoutrade.portfolio_import.csv_import.import_trades_csv`).

Error contract: business validation failures map to
``HTTPException(400, detail={"error_code", "error_type", "error_message",
"hint"})`` so the app-level exception handlers produce the standard payload;
the import layer's ``error_code`` (``invalid_broker`` / ``csv_no_fills`` /
``file_not_found``) is passed through verbatim. Upload-envelope failures use
``file_too_large`` / ``empty_file``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from doyoutrade.debug import emit_debug_event
from doyoutrade.tools._sandbox import knowledge_root

logger = logging.getLogger(__name__)

#: Hard cap for one uploaded statement CSV. Broker exports are small (a busy
#: year is well under 1 MB); 10 MB is generous headroom, not a streaming case.
MAX_STATEMENT_CSV_BYTES = 10 * 1024 * 1024
_CHUNK_SIZE = 1 << 20  # 1 MiB read chunks

#: Static broker suggestions — the broker families the attribution parser's
#: column-alias matrix (`doyoutrade/knowledge/attribution.py::_COLUMN_ALIASES`
#: docstring: 华泰 / 国君 / 银河 / 东财 / 中信) is known to cover. Broker ids are
#: pinyin slugs (used as the ``trades/<broker>/`` directory name).
_SUGGESTED_BROKERS: tuple[tuple[str, str], ...] = (
    ("huatai", "华泰证券"),
    ("guojun", "国泰君安"),
    ("yinhe", "银河证券"),
    ("dongcai", "东方财富"),
    ("zhongxin", "中信证券"),
)


def _http_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    hint: str | None = None,
    error_type: str = "ValueError",
    **extra: Any,
) -> HTTPException:
    """Structured HTTPException matching the app-wide error payload contract."""
    detail: dict[str, Any] = {
        "error_code": error_code,
        "error_type": error_type,
        "error_message": message,
        "hint": hint,
    }
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


async def _read_statement_upload(file: UploadFile) -> bytes:
    """Read a statement upload fully, enforcing the size floor/ceiling.

    Raises structured 400s: ``file_too_large`` past
    :data:`MAX_STATEMENT_CSV_BYTES`, ``empty_file`` for a 0-byte upload.
    """
    data = bytearray()
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_STATEMENT_CSV_BYTES:
            logger.warning(
                "portfolio_import upload rejected: file_too_large filename=%s read=%d limit=%d",
                file.filename, len(data), MAX_STATEMENT_CSV_BYTES,
            )
            raise _http_error(
                400,
                "file_too_large",
                f"statement CSV exceeds {MAX_STATEMENT_CSV_BYTES // (1024 * 1024)} MB limit",
                hint="a broker statement export should be far smaller; check the file",
            )
    if not data:
        logger.warning(
            "portfolio_import upload rejected: empty_file filename=%s", file.filename
        )
        raise _http_error(
            400,
            "empty_file",
            "uploaded statement CSV is empty (0 bytes)",
            hint="re-export / re-upload the broker statement CSV",
        )
    return bytes(data)


def _raise_import_error(result: dict[str, Any]) -> None:
    """Convert an import-layer ``{"status": "error", ...}`` dict into a 400.

    ``error_code`` passes through verbatim (``invalid_broker`` /
    ``csv_no_fills`` / ``file_not_found``); ``unparsed`` details, when present,
    ride along in the detail so the UI can show per-row reasons.
    """
    error_code = str(result.get("error_code") or "csv_import_failed")
    extra: dict[str, Any] = {}
    if result.get("unparsed"):
        extra["unparsed"] = result["unparsed"]
    raise _http_error(
        400,
        error_code,
        str(result.get("message") or "CSV import failed"),
        hint="see error_code / unparsed for the exact rejection reason",
        **extra,
    )


def build_portfolio_import_router() -> APIRouter:
    """Build the ``/portfolio/imports`` router (KB root via ``knowledge_root()``,
    so ``DOYOUTRADE_HOME`` is honoured per request — same anchoring as the
    import layer itself)."""

    router = APIRouter()

    @router.get("/portfolio/imports/brokers")
    async def list_brokers() -> dict[str, Any]:
        """Broker choices: existing ``trades/`` directories ∪ static suggestions.

        A missing / fresh KB (no ``trades/``) returns just the static
        suggestions — a legitimate empty state, never an error.
        """
        trades_root = knowledge_root().expanduser() / "trades"
        suggested = dict(_SUGGESTED_BROKERS)
        existing_names: list[str] = []
        if trades_root.is_dir():
            try:
                existing_names = sorted(
                    p.name for p in trades_root.iterdir() if p.is_dir()
                )
            except OSError as exc:
                # A KB we cannot list is a real fault, not an empty state.
                logger.warning(
                    "portfolio_import brokers: cannot list %s (%s): %s",
                    trades_root, type(exc).__name__, exc,
                )
                raise _http_error(
                    500,
                    "knowledge_base_unreadable",
                    f"cannot list {trades_root}: {type(exc).__name__}: {exc}",
                    hint="check knowledge-base directory permissions",
                    error_type=type(exc).__name__,
                )
        items: list[dict[str, Any]] = [
            {
                "broker": name,
                "display_name": suggested.get(name, name),
                "existing": True,
            }
            for name in existing_names
        ]
        existing_set = set(existing_names)
        items.extend(
            {"broker": broker, "display_name": display, "existing": False}
            for broker, display in _SUGGESTED_BROKERS
            if broker not in existing_set
        )
        return {"items": items}

    @router.post("/portfolio/imports/csv/parse")
    async def parse_statement_csv(
        file: UploadFile = File(...),
        broker: str = Form(...),
    ) -> dict[str, Any]:
        """Zero-write preview: per-fill duplicate marking + counts."""
        data = await _read_statement_upload(file)

        from doyoutrade.portfolio_import.csv_import import analyze_trades_csv

        result = analyze_trades_csv(data, broker=broker)
        if result.get("status") != "ok":
            await emit_debug_event(
                "portfolio_import_parse_rejected",
                {
                    "broker": broker,
                    "filename": file.filename,
                    "error_code": result.get("error_code"),
                    "hint": "fix the upload per error_code and retry the preview",
                },
            )
            _raise_import_error(result)
        logger.info(
            "portfolio_import parse broker=%s filename=%s fills_total=%d new=%d duplicate=%d",
            result["broker"], file.filename, result["fills_total"],
            result["new_count"], result["duplicate_count"],
        )
        await emit_debug_event(
            "portfolio_import_parsed",
            {
                "broker": result["broker"],
                "fills_total": result["fills_total"],
                "new_count": result["new_count"],
                "duplicate_count": result["duplicate_count"],
                "unparsed_count": result["unparsed_count"],
            },
        )
        return result

    @router.post("/portfolio/imports/csv/commit")
    async def commit_statement_csv(
        file: UploadFile = File(...),
        broker: str = Form(...),
        dry_run: bool = Form(False),
    ) -> dict[str, Any]:
        """Import (or ``dry_run``-rehearse) the statement CSV; returns the
        import result verbatim, including the post-import ``review`` block."""
        data = await _read_statement_upload(file)

        from doyoutrade.portfolio_import.csv_import import import_trades_csv

        result = import_trades_csv(data, broker=broker, dry_run=dry_run)
        if result.get("status") != "ok":
            await emit_debug_event(
                "portfolio_import_commit_rejected",
                {
                    "broker": broker,
                    "filename": file.filename,
                    "dry_run": dry_run,
                    "error_code": result.get("error_code"),
                    "hint": "fix the upload per error_code and retry the import",
                },
            )
            _raise_import_error(result)
        logger.info(
            "portfolio_import commit broker=%s filename=%s appended_total=%d "
            "duplicates_skipped=%d dry_run=%s",
            result["broker"], file.filename, result["appended_total"],
            result["duplicates_skipped"], result["dry_run"],
        )
        await emit_debug_event(
            "portfolio_import_committed",
            {
                "broker": result["broker"],
                "appended_total": result["appended_total"],
                "duplicates_skipped": result["duplicates_skipped"],
                "dry_run": result["dry_run"],
                "affected_months": (
                    (result.get("review") or {}).get("affected_months")
                    if result.get("review")
                    else sorted(result.get("written") or {})
                ),
                "attribution_readable": result.get("attribution_readable"),
            },
        )
        return result

    return router


__all__ = ["build_portfolio_import_router", "MAX_STATEMENT_CSV_BYTES"]
