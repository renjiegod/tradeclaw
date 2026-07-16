"""Broker-statement (交割单) CSV import into the private knowledge base.

Feature 6 (docs/dsa-feature-migration.md): the user hands over a broker CSV
export; we normalise it with the existing attribution parser and land the
fills as canonical monthly CSVs under ``trades/<broker>/<YYYY-MM>.csv`` in
``~/.doyoutrade/knowledge`` (no new DB tables — the knowledge ``trades/``
partition is the system of record, and ``read_trade_attribution`` is the
read side).

Parsing deliberately reuses the private internals of
:mod:`doyoutrade.knowledge.attribution` (``_parse_file`` / ``_month_of`` /
``_Fill``): they already encode the multi-broker column-alias matrix
(华泰 / 国君 / 银河 / 东财 / 中信 …), side classification, decimal parsing and
loud row-level skip discipline. Duplicating that here would fork the broker
knowledge; importing the ``_``-private names is the intended reuse (kept in
one module on purpose — see the attribution module docstring).

Dedupe: re-importing an overlapping export appends only new fills. The
dedupe key is ``sha1(date|symbol|side|price|qty)`` with prices/quantities in
normalised decimal form, computed identically for existing on-disk rows and
incoming rows.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from doyoutrade.knowledge.attribution import (
    _Fill,
    _month_of,
    _parse_file,
    read_trade_attribution,
)
from doyoutrade.knowledge.index import build_knowledge_index, write_index_file
from doyoutrade.tools._sandbox import (
    knowledge_root,
    register_knowledge_sandbox,
    resolve_path,
)

logger = logging.getLogger(__name__)

#: Canonical header for the monthly CSVs we write. Every name is an alias
#: recognised by ``attribution._COLUMN_ALIASES``, so the written files are
#: round-trippable through ``read_trade_attribution`` unchanged.
_CANONICAL_HEADER: tuple[str, ...] = (
    "date", "time", "symbol", "name", "side", "price", "qty", "amount",
)

_BROKER_RE = re.compile(r"^[A-Za-z0-9_\-一-鿿]{1,64}$")


def _decimal_str(value: Decimal) -> str:
    """Canonical plain-notation decimal string (``100`` not ``1E+2``, ``10.5`` not ``10.50``)."""
    return format(value.normalize(), "f")


def _fill_dedupe_key(fill: _Fill) -> str:
    """Stable dedupe hash for one fill: date|symbol|side|price|qty."""
    parts = "|".join(
        (
            fill.date,
            fill.symbol,
            fill.side,
            _decimal_str(fill.price),
            _decimal_str(fill.qty),
        )
    )
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()


def _error(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "error",
        "error_code": error_code,
        "message": message,
    }
    out.update(extra)
    return out


def _parse_input(path_or_bytes: str | Path | bytes) -> tuple[list[_Fill], list[dict[str, str]]] | dict[str, Any]:
    """Parse the input CSV into fills via the attribution parser.

    Bytes input is materialised into a temp file so ``_parse_file`` (which
    reads from disk with utf-8-sig handling) can be reused verbatim. Returns
    ``(fills, unparsed)`` or a structured error dict.
    """
    if isinstance(path_or_bytes, (bytes, bytearray)):
        with tempfile.TemporaryDirectory(prefix="doyoutrade-csv-import-") as tmp_dir:
            tmp_path = Path(tmp_dir) / "upload.csv"
            tmp_path.write_bytes(bytes(path_or_bytes))
            return _parse_file(tmp_path, Path(tmp_dir))
    path = Path(path_or_bytes)
    if not path.is_file():
        logger.warning("csv_import: input file not found: %s", path)
        return _error("file_not_found", f"CSV file not found: {path}")
    return _parse_file(path, path.parent)


def _fill_row(fill: _Fill) -> list[str]:
    return [
        fill.date,
        fill.time or "",
        fill.symbol,
        fill.name or "",
        fill.side,
        _decimal_str(fill.price),
        _decimal_str(fill.qty),
        _decimal_str(fill.amount),
    ]


def _validate_broker(broker: str) -> str | dict[str, Any]:
    """Return the cleaned broker name, or a structured ``invalid_broker`` error."""
    broker_clean = str(broker or "").strip()
    if not _BROKER_RE.match(broker_clean):
        logger.warning("csv_import: invalid broker name %r", broker)
        return _error(
            "invalid_broker",
            "broker must be 1-64 chars of letters/digits/_-/中文 (used as a "
            f"directory name); got {broker!r}",
        )
    return broker_clean


def _group_by_month(fills: list[_Fill]) -> dict[str, list[_Fill]]:
    """Group fills by ``YYYY-MM``; loud contract violation on an unbucketable date."""
    by_month: dict[str, list[_Fill]] = {}
    for fill in fills:
        month = _month_of(fill.date)
        if month is None:
            raise ValueError(
                f"fill date {fill.date!r} passed parsing but failed month "
                "bucketing — attribution parser contract violated"
            )
        by_month.setdefault(month, []).append(fill)
    return by_month


def _existing_dedupe_keys(
    target: Path, root: Path, rel: str, unparsed: list[dict[str, str]]
) -> set[str]:
    """Dedupe keys of the parseable rows already on disk at ``target``.

    Rows the attribution parser cannot parse cannot be dedupe-compared; they are
    surfaced into *unparsed* (never silently dropped). Missing file → empty set.
    """
    if not target.is_file():
        return set()
    existing_fills, existing_unparsed = _parse_file(target, root)
    if existing_unparsed:
        unparsed.extend(existing_unparsed)
        logger.warning(
            "csv_import: existing %s has %d unparseable rows; dedupe "
            "only covers parseable rows",
            rel, len(existing_unparsed),
        )
    return {_fill_dedupe_key(f) for f in existing_fills}


#: analyze_trades_csv preview ceilings — the preview is for a UI confirm step,
#: not a bulk export; counts always cover the full file.
_ANALYZE_RECORDS_LIMIT = 500
_ANALYZE_UNPARSED_LIMIT = 20


def analyze_trades_csv(
    path_or_bytes: str | Path | bytes,
    *,
    broker: str,
) -> dict[str, Any]:
    """Pure preview of a broker-statement CSV import — parses and marks each
    fill as duplicate/new against the on-disk monthly files, with ZERO writes
    (no file writes, no directory creation, no index refresh).

    Dedupe semantics are identical to :func:`import_trades_csv` (same
    ``sha1(date|symbol|side|price|qty)`` key, batch-internal duplicates count
    as duplicates too).

    Returns ``{"status": "ok", "broker", "fills_total", "new_count",
    "duplicate_count", "unparsed_count", "records": [...], "records_truncated",
    "unparsed": [...]}``. ``records`` is capped at 500 entries
    (``records_truncated`` set), ``unparsed`` at 20 (``unparsed_count`` is the
    full count). Errors reuse the import error envelope
    (``invalid_broker`` / ``csv_no_fills`` / ``file_not_found``).
    """
    broker_clean = _validate_broker(broker)
    if isinstance(broker_clean, dict):
        return broker_clean

    parsed = _parse_input(path_or_bytes)
    if isinstance(parsed, dict):
        return parsed
    fills, unparsed = parsed

    if not fills:
        logger.warning(
            "csv_import: analyze found no fills (broker=%s, unparsed=%d)",
            broker_clean, len(unparsed),
        )
        return _error(
            "csv_no_fills",
            "no buy/sell fills could be parsed from the CSV; see `unparsed` "
            "for per-file/per-row reasons",
            unparsed=unparsed[:_ANALYZE_UNPARSED_LIMIT],
            unparsed_count=len(unparsed),
        )

    by_month = _group_by_month(fills)
    root = knowledge_root()

    records: list[dict[str, Any]] = []
    new_count = 0
    duplicate_count = 0
    for month in sorted(by_month):
        rel = f"trades/{broker_clean}/{month}.csv"
        target = root / "trades" / broker_clean / f"{month}.csv"
        seen_keys = _existing_dedupe_keys(target, root, rel, unparsed)
        for fill in by_month[month]:
            key = _fill_dedupe_key(fill)
            duplicate = key in seen_keys
            if duplicate:
                duplicate_count += 1
            else:
                new_count += 1
                seen_keys.add(key)  # batch-internal dedupe, same as import
            records.append(
                {
                    "date": fill.date,
                    "time": fill.time or "",
                    "symbol": fill.symbol,
                    "name": fill.name or "",
                    "side": fill.side,
                    "price": _decimal_str(fill.price),
                    "qty": _decimal_str(fill.qty),
                    "amount": _decimal_str(fill.amount),
                    "month": month,
                    "duplicate": duplicate,
                }
            )

    records_truncated = len(records) > _ANALYZE_RECORDS_LIMIT
    logger.info(
        "csv_import: analyze broker=%s fills_total=%d new=%d duplicate=%d unparsed=%d",
        broker_clean, len(fills), new_count, duplicate_count, len(unparsed),
    )
    return {
        "status": "ok",
        "broker": broker_clean,
        "fills_total": len(fills),
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "unparsed_count": len(unparsed),
        "records": records[:_ANALYZE_RECORDS_LIMIT],
        "records_truncated": records_truncated,
        "unparsed": unparsed[:_ANALYZE_UNPARSED_LIMIT],
    }


def import_trades_csv(
    path_or_bytes: str | Path | bytes,
    *,
    broker: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import a broker-statement CSV into ``knowledge/trades/<broker>/<YYYY-MM>.csv``.

    Steps:

    1. Parse via the attribution parser (multi-broker column aliases; every
       unparseable file/row is surfaced in ``unparsed``, never dropped).
    2. Zero parsed fills → ``{"status": "error", "error_code": "csv_no_fills",
       "unparsed": [...]}``.
    3. Group fills by month and write/merge canonical monthly CSVs inside the
       registered knowledge sandbox. Existing rows are dedup-hashed
       (date+symbol+side+price+qty) — overlapping re-imports append only new
       fills and report ``duplicates_skipped``.
    4. Refresh the knowledge index (``_index.md``) and smoke-check the result
       is readable through :func:`read_trade_attribution`.

    ``dry_run=True`` computes the exact same ``written`` / ``duplicates_skipped``
    plan (batch-internal dedupe included) but writes nothing — no files, no
    index refresh, no attribution smoke check. The result then carries
    ``"dry_run": true`` and ``"review": null``.

    A real (non-dry) success additionally carries a ``"review"`` block for
    post-import 复盘: ``{"affected_months": [...], "attribution_summary":
    <read_trade_attribution summary>, "attribution_error": str | None}``.
    Attribution failure never fails the import — ``attribution_summary`` is
    ``None`` and ``attribution_error`` carries type + message (logged loudly).

    Returns ``{"status": "ok", "written": {rel_path: appended_count},
    "duplicates_skipped": N, "fills_total": N, "unparsed": [...],
    "attribution_readable": bool, "dry_run": bool, "review": dict | None}``.
    """
    broker_clean = _validate_broker(broker)
    if isinstance(broker_clean, dict):
        return broker_clean

    parsed = _parse_input(path_or_bytes)
    if isinstance(parsed, dict):
        return parsed
    fills, unparsed = parsed

    if not fills:
        logger.warning(
            "csv_import: no fills parsed from input (broker=%s, unparsed=%d)",
            broker_clean, len(unparsed),
        )
        return _error(
            "csv_no_fills",
            "no buy/sell fills could be parsed from the CSV; see `unparsed` "
            "for per-file/per-row reasons",
            unparsed=unparsed,
        )

    # Group by month; a fill whose date fails month bucketing is impossible
    # here (dates are already ISO-validated by the parser) but guarded loudly.
    by_month = _group_by_month(fills)

    root = knowledge_root()
    if not dry_run:
        register_knowledge_sandbox()  # idempotent; ensures the KB dir + writable sandbox
        broker_dir = root / "trades" / broker_clean
        broker_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, int] = {}
    duplicates_skipped = 0
    for month in sorted(by_month):
        rel = f"trades/{broker_clean}/{month}.csv"
        if dry_run:
            # Read-only plan: no sandbox registration, no directory creation.
            target = root / "trades" / broker_clean / f"{month}.csv"
        else:
            target = resolve_path(str(root / rel))  # raises SandboxViolation on escape

        file_exists = target.is_file()
        existing_keys = _existing_dedupe_keys(target, root, rel, unparsed)

        new_rows: list[list[str]] = []
        for fill in by_month[month]:
            key = _fill_dedupe_key(fill)
            if key in existing_keys:
                duplicates_skipped += 1
                continue
            existing_keys.add(key)  # also dedupes duplicates within the input itself
            new_rows.append(_fill_row(fill))

        if dry_run:
            written[rel] = len(new_rows)
            continue

        if not new_rows and file_exists:
            written[rel] = 0
            logger.info(
                "csv_import: %s all %d fills already present; nothing appended",
                rel, len(by_month[month]),
            )
            continue

        buf = io.StringIO()
        writer = csv.writer(buf)
        if not file_exists:
            writer.writerow(_CANONICAL_HEADER)
        writer.writerows(new_rows)
        mode = "a" if file_exists else "w"
        with open(target, mode, encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        written[rel] = len(new_rows)
        logger.info(
            "csv_import: %s appended=%d duplicates_skipped_so_far=%d (broker=%s)",
            rel, len(new_rows), duplicates_skipped, broker_clean,
        )

    if dry_run:
        logger.info(
            "csv_import: dry_run broker=%s would_append=%d duplicates_skipped=%d",
            broker_clean, sum(written.values()), duplicates_skipped,
        )
        return {
            "status": "ok",
            "broker": broker_clean,
            "written": written,
            "appended_total": sum(written.values()),
            "duplicates_skipped": duplicates_skipped,
            "fills_total": len(fills),
            "unparsed": unparsed,
            "index_path": None,
            "attribution_readable": None,
            "dry_run": True,
            "review": None,
        }

    # Refresh the knowledge index so the new monthly files show up in _index.md.
    index_path: str | None = None
    try:
        index_path = str(write_index_file(build_knowledge_index(root)))
    except OSError as exc:
        # Import succeeded; the navigation map is stale. Loud but non-fatal.
        logger.warning(
            "csv_import: knowledge index refresh failed (%s): %s",
            type(exc).__name__, exc,
        )

    # 复盘融合: after a real import, read back the attribution summary so the
    # caller gets an immediate post-import review. Doubles as the smoke check —
    # the written partition MUST be readable by the attribution read side.
    attribution_readable = True
    attribution_error: str | None = None
    attribution_summary: dict[str, Any] | None = None
    try:
        attribution_summary = read_trade_attribution(root=root)["summary"]
    except Exception as exc:
        attribution_readable = False
        attribution_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "csv_import: read_trade_attribution after import failed (%s): %s",
            type(exc).__name__, exc,
        )

    result: dict[str, Any] = {
        "status": "ok",
        "broker": broker_clean,
        "written": written,
        "appended_total": sum(written.values()),
        "duplicates_skipped": duplicates_skipped,
        "fills_total": len(fills),
        "unparsed": unparsed,
        "index_path": index_path,
        "attribution_readable": attribution_readable,
        "dry_run": False,
        "review": {
            "affected_months": sorted(by_month),
            "attribution_summary": attribution_summary,
            "attribution_error": attribution_error,
        },
    }
    if attribution_error is not None:
        result["attribution_error"] = attribution_error
    return result


__all__ = ["analyze_trades_csv", "import_trades_csv"]
