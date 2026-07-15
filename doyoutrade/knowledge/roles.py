"""个股角色卡 (symbol role cards) — one JSON line per role update.

The user's private KB keeps a prose 角色索引 at ``symbols/roles.md`` (the
agent reads it before strategy matching). This module adds a machine-readable
**伴生存储** ``symbols/roles.jsonl`` — one JSON object per role write — so a
frontend can render structured role cards and the agent can quick-read a
symbol's *current* role without parsing prose.

Schema (append-only, one JSON object per line)::

    {"symbol", "name", "role", "note", "strategy_hint", "updated_at"}

``role`` is a free string; the suggested vocabulary is
``龙头 / 龙二 / 中军 / 补涨 / 杂毛 / 事件型``.

**Why last-wins (append == update).** The agent updates a role by appending a
fresh line via the file primitives (no read-modify-write of the whole file):
the read side de-duplicates by ``symbol``, keeping the *last* line written for
each symbol. This keeps "append to update" simple and robust — the agent never
has to rewrite the file to correct a role.

All paths resolve through ``knowledge_root()`` (honours ``DOYOUTRADE_HOME``) and
go through the KB sandbox (:func:`resolve_path`). The file is part of the
private KB and never enters git / exports / backtest reports.

Discipline (§错误可见性): malformed pre-existing lines are skipped **loudly**
(``logger.info`` with the raw line) rather than crashing; missing values stay
``None`` (never coerced) so a partial card reads as "unknown", not a wrong
value.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from doyoutrade.tools._sandbox import (
    knowledge_root,
    register_knowledge_sandbox,
    resolve_path,
)

logger = logging.getLogger(__name__)

#: The fields persisted per role-card row. ``symbol`` is the de-dup key; the
#: rest describe the card. Kept as the stable API/storage projection so the
#: surface is fixed even if a hand-edited row carries extra keys.
_ROLE_FIELDS: tuple[str, ...] = (
    "symbol",
    "name",
    "role",
    "note",
    "strategy_hint",
    "updated_at",
)

#: Relative path of the structured role store within the KB, and the sibling
#: prose index it accompanies (``roles.md`` stays untouched as the narrative).
_ROLES_JSONL_REL = "symbols/roles.jsonl"


def _roles_path(root: Path) -> Path:
    return root / "symbols" / "roles.jsonl"


def read_symbol_roles(*, root: Path | None = None) -> dict[str, Any]:
    """Read ``symbols/roles.jsonl``, de-dup by symbol (last-wins), and sort.

    Reads the append-only role log, keeps only the **last** line written for
    each ``symbol`` (last-wins — a later append supersedes an earlier one so
    the agent can "append to update"), and returns
    ``{"items": [{symbol, name, role, note, strategy_hint, updated_at}]}``
    sorted by ``updated_at`` descending (rows without ``updated_at`` sort to
    the end, then by ``symbol`` ascending for a stable order).

    A fresh KB / absent file returns ``{"items": []}`` (a legitimate "nothing
    here yet" state, not an error). Malformed lines (hand-edit / partial write)
    are skipped **loudly** (``logger.info`` with the raw line) rather than
    crashing the read — one bad line must not take down the whole card set.

    ``root`` defaults to ``knowledge_root()`` but callers holding their own KB
    root resolver (e.g. the ``/knowledge`` API router) pass it explicitly so
    the read stays anchored to the same base as the rest of that surface.
    """
    if root is None:
        root = knowledge_root()
    path = _roles_path(root)
    if not path.is_file():
        return {"items": []}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # A file present but unreadable is a real problem — surface it loudly
        # (the API layer maps a raised error to a 500; the read side does not
        # silently pretend the store is empty).
        logger.warning(
            "symbol_roles read failed %s (%s): %s",
            path, type(exc).__name__, exc,
        )
        raise

    # Preserve write order so "last line wins" per symbol. dict keeps insertion
    # order, and re-assigning a key keeps its original position; we don't rely
    # on position for output (we sort), only on last-wins value replacement.
    latest: dict[str, dict[str, Any]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.info(
                "symbol_roles skipping malformed line reason=json_decode (%s) raw=%r",
                exc, raw,
            )
            continue
        if not isinstance(obj, dict):
            logger.info(
                "symbol_roles skipping non-object line reason=not_object raw=%r",
                raw,
            )
            continue
        symbol = obj.get("symbol")
        if not symbol:
            logger.info(
                "symbol_roles skipping row without symbol raw=%r", raw
            )
            continue
        # Project onto the fixed schema (missing values stay None — never
        # coerced) so extra hand-edited keys don't leak into the API surface.
        latest[str(symbol)] = {field: obj.get(field) for field in _ROLE_FIELDS}

    items = list(latest.values())
    # updated_at descending (newest first); missing updated_at sorts last;
    # ties broken by symbol ascending for a stable, deterministic order.
    items.sort(key=lambda r: str(r.get("symbol") or ""))
    items.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return {"items": items}


def upsert_symbol_role(
    symbol: str,
    role: str,
    *,
    name: str | None = None,
    note: str | None = None,
    strategy_hint: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Idempotently upsert one symbol's role card into ``symbols/roles.jsonl``.

    Appends a fresh ``{symbol, name, role, note, strategy_hint, updated_at}``
    line; the read side (:func:`read_symbol_roles`) de-duplicates by ``symbol``
    with last-wins, so a repeat write for the same ``symbol`` supersedes the
    earlier card without duplicating it in the reader. To keep the on-disk file
    from growing without bound (and so a direct file read stays sane) this
    helper also drops any pre-existing line for the same ``symbol`` while
    rewriting — i.e. read-modify-write with the identical last-wins semantics
    the reader uses.

    ``updated_at`` is stored verbatim (the library never calls
    ``datetime.now()``): the caller passes the current time at runtime; tests
    pass a fixed value. Omitted / ``None`` values stay ``None`` on the row
    (never coerced) so a partial card reads as "unknown".

    All paths resolve through ``knowledge_root()`` and go through the KB
    sandbox (:func:`resolve_path`); the file is private KB memory and never
    enters git / exports / backtest reports.

    Malformed pre-existing lines are skipped **loudly** (``logger.info``) and
    dropped so the file self-heals.

    Returns ``{path, upserted, replaced, row_count, dropped}`` — a structured
    result the caller can turn into a debug event (this helper is sync).
    """
    register_knowledge_sandbox()  # idempotent: ensures KB dir + writable sandbox
    root = knowledge_root()
    target = _roles_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Sandbox safety check (raises SandboxViolation if outside the KB root).
    resolved = resolve_path(str(target))

    new_row: dict[str, Any] = {
        "symbol": str(symbol),
        "name": name,
        "role": role,
        "note": note,
        "strategy_hint": strategy_hint,
        "updated_at": updated_at,
    }
    key = new_row["symbol"]

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
                    "symbol_roles upsert skipping malformed line reason=json_decode "
                    "(%s) raw=%r",
                    exc, raw,
                )
                continue
            if not isinstance(obj, dict):
                dropped += 1
                logger.info(
                    "symbol_roles upsert skipping non-object line reason=not_object raw=%r",
                    raw,
                )
                continue
            if str(obj.get("symbol") or "") == key:
                replaced = True
                continue  # drop the stale same-symbol row; the fresh one is appended
            rows.append(obj)

    rows.append(new_row)

    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    resolved.write_text(body, encoding="utf-8")

    return {
        "path": target.relative_to(root).as_posix(),
        "upserted": True,
        "replaced": replaced,
        "row_count": len(rows),
        "dropped": dropped,
    }
