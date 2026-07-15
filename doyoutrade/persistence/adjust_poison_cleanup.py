"""Startup cleanup for poisoned qfq rows caused by an inverted adjust mapping.

The bad baostock mapping wrote qfq rows with back-adjusted prices
(``adjustflag=1``) instead of front-adjusted prices (``adjustflag=2``). The
result is a qfq series that can be ~10x larger than the sibling none series on
the same trading day. Those rows poison both:

* ``market_bars`` / ``market_bar_sync_state`` — local-first K-line reads
* ``cached_bars`` / ``cached_bar_ranges`` — backtest/live persistent cache

We detect the poison conservatively by pairing qfq and none rows for the same
``(provider, symbol, interval, day)`` and flagging keys whose qfq close is far
above the none close. Legitimate front-adjusted prices should not be many
times *higher* than the raw close on the same day.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa

from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)

QFQ_POISON_RATIO_THRESHOLD = 5.0


async def purge_poisoned_qfq_rows(
    runtime_session_factory: Any,
    market_session_factory: Any,
) -> dict[str, int]:
    """Delete obviously poisoned qfq rows across market and cache storage.

    Returns:
    ``{
        "poisoned_keys": int,
        "poisoned_symbols": int,
        "deleted_market_bars": int,
        "deleted_market_sync_state": int,
        "deleted_cached_bars": int,
        "deleted_cached_ranges": int,
      }``
    """
    poisoned_rows_sql = sa.text(
        """
        SELECT q.provider, q.symbol, q.interval
        FROM market_bars q
        JOIN market_bars n
          ON n.provider = q.provider
         AND n.symbol = q.symbol
         AND n.interval = q.interval
         AND n.bar_time = q.bar_time
         AND n.adjust = 'none'
        WHERE q.adjust = 'qfq'
          AND n.close_price > 0
          AND q.close_price / n.close_price > :ratio_threshold
        GROUP BY q.provider, q.symbol, q.interval
        """
    )
    delete_market_bars_sql = sa.text(
        """
        DELETE FROM market_bars
        WHERE provider = :provider
          AND symbol = :symbol
          AND interval = :interval
          AND adjust = 'qfq'
        """
    )
    delete_market_sync_state_sql = sa.text(
        """
        DELETE FROM market_bar_sync_state
        WHERE provider = :provider
          AND symbol = :symbol
          AND interval = :interval
          AND adjust = 'qfq'
        """
    )
    delete_cached_bars_sql = sa.text(
        """
        DELETE FROM cached_bars
        WHERE symbol = :symbol
          AND interval = :interval
          AND adjust = 'qfq'
        """
    )
    delete_cached_ranges_sql = sa.text(
        """
        DELETE FROM cached_bar_ranges
        WHERE symbol = :symbol
          AND interval = :interval
          AND adjust = 'qfq'
        """
    )

    async with market_session_factory() as market_session:
        poisoned_keys = [
            tuple(row)
            for row in (
                await market_session.execute(
                    poisoned_rows_sql,
                    {"ratio_threshold": QFQ_POISON_RATIO_THRESHOLD},
                )
            ).all()
        ]

    if not poisoned_keys:
        return {
            "poisoned_keys": 0,
            "poisoned_symbols": 0,
            "deleted_market_bars": 0,
            "deleted_market_sync_state": 0,
            "deleted_cached_bars": 0,
            "deleted_cached_ranges": 0,
        }

    deleted_market_bars = 0
    deleted_market_sync_state = 0
    async with market_session_factory() as market_session:
        for provider, symbol, interval in poisoned_keys:
            deleted_market_bars += (
                await market_session.execute(
                    delete_market_bars_sql,
                    {"provider": provider, "symbol": symbol, "interval": interval},
                )
            ).rowcount or 0
            deleted_market_sync_state += (
                await market_session.execute(
                    delete_market_sync_state_sql,
                    {"provider": provider, "symbol": symbol, "interval": interval},
                )
            ).rowcount or 0
        await market_session.commit()

    unique_symbol_intervals = sorted({(symbol, interval) for _, symbol, interval in poisoned_keys})
    deleted_cached_bars = 0
    deleted_cached_ranges = 0
    async with runtime_session_factory() as runtime_session:
        for symbol, interval in unique_symbol_intervals:
            deleted_cached_bars += (
                await runtime_session.execute(
                    delete_cached_bars_sql,
                    {"symbol": symbol, "interval": interval},
                )
            ).rowcount or 0
            deleted_cached_ranges += (
                await runtime_session.execute(
                    delete_cached_ranges_sql,
                    {"symbol": symbol, "interval": interval},
                )
            ).rowcount or 0
        await runtime_session.commit()

    result = {
        "poisoned_keys": len(poisoned_keys),
        "poisoned_symbols": len({symbol for _, symbol, _ in poisoned_keys}),
        "deleted_market_bars": deleted_market_bars,
        "deleted_market_sync_state": deleted_market_sync_state,
        "deleted_cached_bars": deleted_cached_bars,
        "deleted_cached_ranges": deleted_cached_ranges,
    }
    logger.warning(
        "purged poisoned qfq rows poisoned_keys=%s poisoned_symbols=%s "
        "deleted_market_bars=%s deleted_market_sync_state=%s "
        "deleted_cached_bars=%s deleted_cached_ranges=%s ratio_threshold=%s",
        result["poisoned_keys"],
        result["poisoned_symbols"],
        result["deleted_market_bars"],
        result["deleted_market_sync_state"],
        result["deleted_cached_bars"],
        result["deleted_cached_ranges"],
        QFQ_POISON_RATIO_THRESHOLD,
    )
    await emit_debug_event(
        "adjust_qfq_poison_purged",
        {
            **result,
            "ratio_threshold": QFQ_POISON_RATIO_THRESHOLD,
            "reason": "baostock_adjustflag_qfq_hfq_inverted",
            "hint": (
                "poisoned qfq rows were deleted from market_bars and cached_bars; "
                "the next local miss or sync run will refill them with the corrected mapping"
            ),
        },
    )
    return result
