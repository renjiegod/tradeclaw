"""Watchlist-token universe resolution (eager, observable).

A task ``universe`` may contain ``@watchlist:<tag>`` tokens alongside plain
symbols. ``@watchlist:核心池`` expands to the symbols carrying that tag and
``@watchlist:*`` expands to the whole watchlist. The expansion happens **eagerly
at worker assembly time** (Phase B calls :func:`resolve_watchlist_universe` on
the async assembly path, before ``build_trading_data_stack`` needs concrete
symbols), so the resolved symbol list is visible and deterministic for the
cycle. ``cycle_task_config_from_params`` deliberately leaves the tokens raw —
this module is the single resolution point.

Error visibility (CLAUDE.md "错误可见性"):

- Resolution always emits a structured ``watchlist_universe_resolved`` event,
  *including the zero-symbol case*, so "task ran but bought nothing because the
  watchlist tag was empty" is never silent.
- Repository failures are not swallowed: a structured ``watchlist_universe_resolve_failed``
  event is emitted (when an ``emit`` callback is provided) and the exception is
  re-raised so the cycle fails visibly rather than running on a truncated
  universe.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Protocol

logger = logging.getLogger(__name__)

#: Prefix marking a watchlist-tag token inside a task universe.
WATCHLIST_TOKEN_PREFIX = "@watchlist:"

#: The tag that expands to the entire watchlist (no tag filter).
WATCHLIST_ALL_TAG = "*"

# emit(event_name, payload) — optional observability sink. Phase B passes a
# callback that fans out to OTel span events + emit_debug_event.
EmitCallback = Callable[[str, dict[str, Any]], None]


class _WatchlistSymbolSource(Protocol):
    """Minimal repo surface this module needs: tag → symbols (async)."""

    async def list_symbols(self, tag: str | None = ...) -> list[str]: ...


def split_universe_tokens(universe: Iterable[str]) -> tuple[list[str], list[str]]:
    """Partition a universe into ``(plain_symbols, tags)``.

    - ``"@watchlist:核心池"`` → tag ``"核心池"``.
    - ``"@watchlist:*"`` → tag ``"*"`` (sentinel for "the whole watchlist").
    - anything else → a plain symbol.

    Order is preserved within each bucket and entries are de-duplicated
    (first occurrence wins), so the function is a pure, single-pass transform
    suitable for unit testing.
    """
    plain_symbols: list[str] = []
    tags: list[str] = []
    for raw in universe:
        token = str(raw).strip()
        if not token:
            continue
        if token.startswith(WATCHLIST_TOKEN_PREFIX):
            tag = token[len(WATCHLIST_TOKEN_PREFIX):].strip()
            # Empty tag (bare "@watchlist:") is treated as "all".
            tag = tag or WATCHLIST_ALL_TAG
            if tag not in tags:
                tags.append(tag)
        else:
            if token not in plain_symbols:
                plain_symbols.append(token)
    return plain_symbols, tags


async def resolve_watchlist_universe(
    universe: Iterable[str],
    repo: _WatchlistSymbolSource,
    *,
    emit: EmitCallback | None = None,
) -> list[str]:
    """Expand ``@watchlist:<tag>`` tokens in ``universe`` into concrete symbols.

    For each tag, ``await repo.list_symbols(tag)`` is called (``None`` for the
    ``*`` "all" sentinel). Resolved tag symbols are appended after the plain
    symbols and the combined list is de-duplicated **preserving order**.

    A ``watchlist_universe_resolved`` event is always emitted (even when the
    result is empty) via the optional ``emit`` callback. Repository errors are
    surfaced — a ``watchlist_universe_resolve_failed`` event is emitted and the
    exception re-raised, never swallowed.
    """
    plain_symbols, tags = split_universe_tokens(universe)

    # Fast path: no watchlist tokens — return plain symbols untouched, no event
    # (nothing was resolved, the universe is already concrete).
    if not tags:
        return list(plain_symbols)

    resolved: list[str] = list(plain_symbols)
    seen: set[str] = set(plain_symbols)
    for tag in tags:
        lookup_tag = None if tag == WATCHLIST_ALL_TAG else tag
        try:
            tag_symbols = await repo.list_symbols(lookup_tag)
        except Exception as exc:
            if emit is not None:
                emit(
                    "watchlist_universe_resolve_failed",
                    {
                        "tag": tag,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "source": "watchlist_universe",
                        "hint": (
                            "watchlist_repository.list_symbols raised while "
                            "resolving an @watchlist: universe token; the cycle "
                            "cannot run on a truncated universe."
                        ),
                    },
                )
            logger.warning(
                "watchlist universe resolve failed tag=%s %s: %s",
                tag,
                type(exc).__name__,
                exc,
            )
            raise
        for symbol in tag_symbols:
            sym = str(symbol).strip()
            if sym and sym not in seen:
                seen.add(sym)
                resolved.append(sym)

    if emit is not None:
        emit(
            "watchlist_universe_resolved",
            {
                "tags": tags,
                "resolved_count": len(resolved),
                "plain_count": len(plain_symbols),
                "source": "watchlist_universe",
                "hint": (
                    "watchlist tag(s) resolved to 0 symbols; add stocks via "
                    "`doyoutrade-cli watchlist add <symbol> --tags <tag>`."
                    if len(resolved) == 0
                    else ""
                ),
            },
        )
    logger.info(
        "watchlist universe resolved tags=%s plain=%d resolved=%d",
        tags,
        len(plain_symbols),
        len(resolved),
    )
    return resolved


__all__ = [
    "WATCHLIST_TOKEN_PREFIX",
    "WATCHLIST_ALL_TAG",
    "split_universe_tokens",
    "resolve_watchlist_universe",
]
