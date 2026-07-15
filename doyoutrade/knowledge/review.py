"""Daily-review knowledge-base read + write-back helpers.

Read side (:func:`build_daily_review_knowledge_digest`): assemble a compact,
bounded digest of the user's private KB relevant to a day's review — the
navigation map, the most-recent prior 复盘 journal, the ``symbols/roles.md``
role index, the month's ``cycles`` overview, and the month's broker-exported
交割单 CSV — for injection as review ``pre_data``.

Write side (:func:`write_daily_review_journal`): persist the generated 复盘 to
``journal/<YYYY>/<YYYY-MM-DD>.md`` with a guaranteed index-friendly ``# ``
title (synthesized if the model omitted one — otherwise the knowledge index
degrades the entry to a weak filename stem), NEVER silently overwriting an
existing same-day entry (a second fire is appended as a timestamped section),
and refreshing ``_index.md`` afterwards (a once-daily write is an appropriate
refresh point per the knowledge index design).

All paths resolve through ``knowledge_root()`` (honours ``DOYOUTRADE_HOME``);
writes go through the registered KB sandbox + :func:`resolve_path`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from doyoutrade.knowledge.index import (
    build_knowledge_index,
    render_index_markdown,
    write_index_file,
)
from doyoutrade.tools._sandbox import (
    knowledge_root,
    register_knowledge_sandbox,
    resolve_path,
)

logger = logging.getLogger(__name__)

#: Cap per-file text read so the digest stays O(files), not O(bytes).
_EXCERPT_BYTES = 8192
#: Cap parsed CSV rows so a large monthly 交割单 export cannot blow the prompt.
_MAX_CSV_ROWS = 300

#: The five numeric/label fields persisted per trading day in a sentiment log
#: row. ``date`` is the upsert key; the rest describe the day's 情绪周期 state.
_SENTIMENT_FIELDS: tuple[str, ...] = (
    "date",
    "label",
    "limit_up_count",
    "limit_down_count",
    "broken_board_count",
    "broken_board_rate",
    "max_streak",
)


def _read_excerpt(
    path: Path, errors: list[dict[str, str]], *, cap: int = _EXCERPT_BYTES
) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "daily_review digest: read failed %s (%s): %s",
            path,
            type(exc).__name__,
            exc,
        )
        errors.append(
            {
                "stage": f"read:{path.name}",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "hint": "knowledge file unreadable; digest omits it",
            }
        )
        return None
    if len(raw) > cap:
        return raw[:cap] + "\n…(truncated)"
    return raw


def _latest_prior_journal(root: Path, asof: date) -> Path | None:
    """Most-recent ``journal/<YYYY>/<stem>.md`` whose stem sorts strictly before
    ``asof`` (so the review reads PRIOR context, not the file it is about to
    write). Names are date-like, so lexical-desc ≈ date-desc."""
    journal_root = root / "journal"
    if not journal_root.is_dir():
        return None
    asof_stem = asof.isoformat()
    candidates: list[Path] = []
    for path in journal_root.rglob("*.md"):
        if not path.is_file():
            continue
        if path.stem < asof_stem:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stem)


def _month_trades_csv(root: Path, asof: date) -> Path | None:
    """First ``trades/**/*.csv`` whose path carries the ``YYYY-MM`` of ``asof``
    (skill naming: ``trades/<broker>/<YYYY-MM>.csv``)."""
    trades_root = root / "trades"
    if not trades_root.is_dir():
        return None
    month = f"{asof.year:04d}-{asof.month:02d}"
    for path in sorted(trades_root.rglob("*.csv")):
        if path.is_file() and month in path.as_posix():
            return path
    return None


def _parse_csv(path: Path, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "daily_review digest: trades csv read failed %s (%s): %s",
            path,
            type(exc).__name__,
            exc,
        )
        errors.append(
            {
                "stage": "trades_csv",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "hint": "broker 交割单 CSV unreadable; review uses live QMT trades only",
            }
        )
        return None
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    columns = rows[0]
    data_rows = rows[1:]
    truncated = len(data_rows) > _MAX_CSV_ROWS
    return {
        "columns": columns,
        "rows": data_rows[:_MAX_CSV_ROWS],
        "row_count": len(data_rows),
        "truncated": truncated,
    }


def build_daily_review_knowledge_digest(asof: date) -> dict[str, Any]:
    """Assemble the KB portion of the daily-review ``pre_data``.

    Returns a dict with ``root_exists`` plus (when present) ``index_markdown``
    (the navigation map), ``latest_journal`` ({path, content}), ``symbols_roles``
    ({path, content}), ``cycles_overview`` ({path, content}), ``trades_csv``
    ({path, columns, rows, row_count, truncated}), and an ``errors`` list of any
    read failures (surfaced, not swallowed).
    """
    root = knowledge_root()
    errors: list[dict[str, str]] = []
    if not root.is_dir():
        return {
            "root_exists": False,
            "index_markdown": "",
            "latest_journal": None,
            "symbols_roles": None,
            "cycles_overview": None,
            "trades_csv": None,
            "errors": errors,
        }

    try:
        index_markdown = render_index_markdown(build_knowledge_index(root))
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        logger.warning(
            "daily_review digest: index build failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        errors.append(
            {
                "stage": "index",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "hint": "knowledge index build failed; review proceeds without the nav map",
            }
        )
        index_markdown = ""

    latest_journal = None
    jp = _latest_prior_journal(root, asof)
    if jp is not None:
        content = _read_excerpt(jp, errors)
        if content is not None:
            latest_journal = {"path": jp.relative_to(root).as_posix(), "content": content}

    symbols_roles = None
    roles = root / "symbols" / "roles.md"
    if roles.is_file():
        content = _read_excerpt(roles, errors)
        if content is not None:
            symbols_roles = {"path": "symbols/roles.md", "content": content}

    cycles_overview = None
    overview = root / "cycles" / f"{asof.year:04d}-{asof.month:02d}" / "_overview.md"
    if overview.is_file():
        content = _read_excerpt(overview, errors)
        if content is not None:
            cycles_overview = {
                "path": overview.relative_to(root).as_posix(),
                "content": content,
            }

    trades_csv = None
    csv_path = _month_trades_csv(root, asof)
    if csv_path is not None:
        parsed = _parse_csv(csv_path, errors)
        if parsed is not None:
            trades_csv = {"path": csv_path.relative_to(root).as_posix(), **parsed}

    return {
        "root_exists": True,
        "index_markdown": index_markdown,
        "latest_journal": latest_journal,
        "symbols_roles": symbols_roles,
        "cycles_overview": cycles_overview,
        "trades_csv": trades_csv,
        "errors": errors,
    }


def _strip_leading_title(text: str) -> str:
    """Drop a leading ``# `` heading line so an appended section does not add a
    second H1 under the day's existing journal title."""
    stripped = text.lstrip()
    if stripped.startswith("# "):
        parts = stripped.split("\n", 1)
        return parts[1].lstrip("\n") if len(parts) > 1 else ""
    return stripped


def _ensure_title(content: str, asof: date) -> tuple[str, bool]:
    """Guarantee an index-friendly title. Returns ``(body, synthesized)``.

    A leading ``# `` heading or YAML ``---`` front-matter satisfies the
    knowledge index; otherwise a ``# <YYYY-MM-DD> 复盘`` heading is prepended so
    the entry never degrades to a weak filename-stem title.
    """
    stripped = content.strip()
    first_line = stripped.split("\n", 1)[0] if stripped else ""
    if first_line.startswith("# ") or stripped.startswith("---"):
        return stripped + "\n", False
    title = f"# {asof.isoformat()} 复盘"
    body = f"{title}\n\n{stripped}\n" if stripped else f"{title}\n"
    return body, True


def write_daily_review_journal(
    asof: date,
    content: str,
    *,
    fired_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist the generated 复盘 to ``journal/<YYYY>/<YYYY-MM-DD>.md``.

    Never silently overwrites an existing same-day entry: a repeat fire appends
    a timestamped ``## 复盘补充 HH:MM`` section to the existing file. Synthesizes
    a ``# `` title when missing. Refreshes ``_index.md`` after writing.

    Returns ``{path, bytes_written, appended, title_synthesized,
    index_refreshed}`` — a structured result the caller turns into a debug
    event (this helper is sync; the executor emits the event in async context).
    """
    register_knowledge_sandbox()  # idempotent: ensures KB dir + writable sandbox
    root = knowledge_root()
    rel = f"journal/{asof.year:04d}/{asof.isoformat()}.md"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # Sandbox safety check (raises SandboxViolation if outside the KB root).
    resolved = resolve_path(str(target))

    body, title_synthesized = _ensure_title(content, asof)
    appended = False
    if resolved.exists():
        existing = resolved.read_text(encoding="utf-8").rstrip()
        stamp = (fired_at or datetime.now()).strftime("%H:%M")
        body = (
            f"{existing}\n\n---\n\n## 复盘补充 {stamp}\n\n"
            f"{_strip_leading_title(body)}".rstrip()
            + "\n"
        )
        appended = True

    encoded = body.encode("utf-8")
    resolved.write_text(body, encoding="utf-8")

    index_refreshed = False
    try:
        write_index_file(build_knowledge_index(root))
        index_refreshed = True
    except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
        logger.warning(
            "daily_review journal: index refresh failed after write (%s): %s",
            type(exc).__name__,
            exc,
        )

    return {
        "path": rel,
        "bytes_written": len(encoded),
        "appended": appended,
        "title_synthesized": title_synthesized,
        "index_refreshed": index_refreshed,
    }


# ---------------------------------------------------------------------------
# 情绪周期日志 (sentiment cycle log) — one JSON line per trading day.
# ---------------------------------------------------------------------------
#
# The daily review, once it has successfully gathered the day's whole-market
# 情绪 (limit-up/down/broken-board breadth + a rule label), appends/updates a
# single row to ``cycles/<YYYY-MM>/_sentiment.jsonl``. This is a machine-readable
# sidecar to the prose ``cycles/<month>/_overview.md`` notes — it lets a
# frontend render a continuous 情绪周期 timeline (退潮 → 分歧 → 发酵 → 高潮)
# without re-scraping the market. It lives under ``knowledge_root()`` so it
# shares the KB privacy boundary (never git / export / backtest reports).


def _sentiment_month_path(root: Path, asof: date) -> Path:
    return root / "cycles" / f"{asof.year:04d}-{asof.month:02d}" / "_sentiment.jsonl"


def _normalize_sentiment_row(asof: date, breadth: dict[str, Any]) -> dict[str, Any]:
    """Project ``breadth`` onto the fixed sentiment-row schema.

    ``breadth`` carries the aggregates the market-breadth provider / tool
    produce (``label`` / ``limit_up_count`` / ``limit_down_count`` /
    ``broken_board_count`` / ``broken_board_rate`` / ``max_streak``). Only the
    documented fields are kept; anything else is dropped. ``date`` is always
    taken from ``asof`` (never from the payload) so the upsert key is
    authoritative. Missing numeric fields stay ``None`` (never coerced to 0 —
    §错误可见性) so a partial day reads as "unknown", not as a flat zero day.
    """
    row: dict[str, Any] = {"date": asof.isoformat()}
    for field in _SENTIMENT_FIELDS:
        if field == "date":
            continue
        row[field] = breadth.get(field)
    return row


def upsert_sentiment_log(asof: date, breadth: dict[str, Any]) -> dict[str, Any]:
    """Idempotently upsert one trading day's 情绪 row into the month's log.

    Writes/updates ``cycles/<YYYY-MM>/_sentiment.jsonl`` — one JSON object per
    trading day, keyed by ``date``. A repeat fire for the same ``asof``
    **replaces** that day's row in place (never duplicates it) and leaves every
    other day untouched. Rows stay in ascending ``date`` order.

    Malformed pre-existing lines (a hand-edit / partial write) are skipped
    **loudly** (``logger.info`` with the raw line) rather than crashing the
    upsert — the caller's fresh row still lands, and the bad line is dropped so
    the file self-heals.

    All paths resolve through ``knowledge_root()`` and go through the KB
    sandbox (:func:`resolve_path`); the file is part of the private KB and never
    enters git / exports / backtest reports.

    Returns ``{path, upserted, replaced, row_count, dropped}`` — a structured
    result the caller turns into a debug event (this helper is sync).
    """
    register_knowledge_sandbox()  # idempotent: ensures KB dir + writable sandbox
    root = knowledge_root()
    target = _sentiment_month_path(root, asof)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Sandbox safety check (raises SandboxViolation if outside the KB root).
    resolved = resolve_path(str(target))

    new_row = _normalize_sentiment_row(asof, breadth)
    key = new_row["date"]

    rows: list[dict[str, Any]] = []
    replaced = False
    dropped = 0
    if resolved.exists():
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                dropped += 1
                logger.info(
                    "sentiment_log skipping malformed line month=%s reason=json_decode "
                    "(%s) raw=%r",
                    target.parent.name, exc, raw,
                )
                continue
            if not isinstance(obj, dict):
                dropped += 1
                logger.info(
                    "sentiment_log skipping non-object line month=%s reason=not_object raw=%r",
                    target.parent.name, raw,
                )
                continue
            if obj.get("date") == key:
                replaced = True
                continue  # drop the stale same-day row; the fresh one is appended
            rows.append(obj)

    rows.append(new_row)
    rows.sort(key=lambda r: str(r.get("date") or ""))

    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    resolved.write_text(body, encoding="utf-8")

    return {
        "path": target.relative_to(root).as_posix(),
        "upserted": True,
        "replaced": replaced,
        "row_count": len(rows),
        "dropped": dropped,
    }


def read_sentiment_timeline(
    months: int = 3, *, root: Path | None = None
) -> dict[str, Any]:
    """Read + merge every ``cycles/*/_sentiment.jsonl`` into one sorted timeline.

    Walks all monthly sentiment logs under ``cycles/``, merges their rows, sorts
    ascending by ``date``, and keeps only rows whose ``date`` falls in the most
    recent ``months`` calendar months relative to the newest recorded day (a
    self-relative window so it works regardless of "today"). Returns
    ``{"items": [...]}`` — an empty list when the KB / logs are absent (a fresh
    KB is a legitimate "nothing here yet" state, not an error).

    ``root`` defaults to ``knowledge_root()`` but callers holding their own KB
    root resolver (e.g. the ``/knowledge`` API router) pass it explicitly so the
    read stays anchored to the same base the rest of that surface uses.

    Malformed lines are skipped **loudly** (``logger.info``) rather than
    crashing — one bad hand-edit must not take down the whole timeline.
    """
    if months < 1:
        months = 1
    if root is None:
        root = knowledge_root()
    cycles_root = root / "cycles"
    if not cycles_root.is_dir():
        return {"items": []}

    items: list[dict[str, Any]] = []
    for path in sorted(cycles_root.glob("*/_sentiment.jsonl")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.info(
                "sentiment_timeline skipping unreadable log %s (%s): %s",
                path, type(exc).__name__, exc,
            )
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.info(
                    "sentiment_timeline skipping malformed line in %s reason=json_decode "
                    "(%s) raw=%r",
                    path.name, exc, raw,
                )
                continue
            if not isinstance(obj, dict) or not obj.get("date"):
                logger.info(
                    "sentiment_timeline skipping row without date in %s raw=%r",
                    path.name, raw,
                )
                continue
            # Project onto the fixed schema so the API surface is stable even if
            # a log row carries extra fields.
            items.append({field: obj.get(field) for field in _SENTIMENT_FIELDS})

    if not items:
        return {"items": []}

    items.sort(key=lambda r: str(r.get("date") or ""))

    # Self-relative N-month window anchored on the newest recorded day: keep
    # rows whose date >= (newest_month - (months - 1)) start-of-month.
    newest = items[-1].get("date")
    try:
        anchor = date.fromisoformat(str(newest))
    except (TypeError, ValueError):
        # Newest row has an unparseable date — return everything sorted rather
        # than silently dropping data on a window we cannot compute.
        logger.info(
            "sentiment_timeline: newest row date unparseable (%r); returning full timeline",
            newest,
        )
        return {"items": items}

    total_month = anchor.year * 12 + (anchor.month - 1)
    cutoff_month = total_month - (months - 1)
    cutoff_year, cutoff_mon = divmod(cutoff_month, 12)
    cutoff = date(cutoff_year, cutoff_mon + 1, 1)

    kept: list[dict[str, Any]] = []
    for row in items:
        try:
            d = date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError):
            logger.info(
                "sentiment_timeline skipping row with unparseable date raw=%r", row
            )
            continue
        if d >= cutoff:
            kept.append(row)
    return {"items": kept}
