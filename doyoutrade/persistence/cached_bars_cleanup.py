"""One-time / startup cleanup for poisoned ``cached_bars`` rows.

Migration ``20260609_02`` relabeled legacy qfq rows as ``adjust='none'`` without
re-fetching prices.  A-share daily none bars with closes above
:const:`POISONED_NONE_DAILY_CLOSE_THRESHOLD` are almost certainly that legacy
poison (e.g. 风华高科 595→58 复权断崖).  When detected we wipe the persistent
cache so backtests re-fetch from upstream.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)

POISONED_NONE_DAILY_CLOSE_THRESHOLD = 200.0


async def purge_poisoned_cached_bars(session_factory: Any) -> dict[str, int]:
    """Delete persistent cached bars when legacy qfq-as-none poison is detected.

    Returns counts: ``{"poisoned_rows": int, "deleted_bars": int, "deleted_ranges": int}``.
    """
    async with session_factory() as session:
        poisoned = int(
            (
                await session.execute(
                    sa.text(
                        """
                        SELECT COUNT(*) FROM cached_bars
                        WHERE adjust = 'none'
                          AND interval = '1d'
                          AND close_price > :threshold
                        """
                    ),
                    {"threshold": POISONED_NONE_DAILY_CLOSE_THRESHOLD},
                )
            ).scalar_one()
        )
        if poisoned == 0:
            return {"poisoned_rows": 0, "deleted_bars": 0, "deleted_ranges": 0}

        bars_deleted = (
            await session.execute(sa.text("DELETE FROM cached_bars"))
        ).rowcount or 0
        ranges_deleted = (
            await session.execute(sa.text("DELETE FROM cached_bar_ranges"))
        ).rowcount or 0
        await session.commit()

        logger.warning(
            "purged poisoned cached_bars poisoned_rows=%s deleted_bars=%s "
            "deleted_ranges=%s reason=legacy_qfq_relabeled_as_none",
            poisoned,
            bars_deleted,
            ranges_deleted,
        )
        await emit_debug_event(
            "cached_bars_purged_legacy_adjust",
            {
                "reason": "legacy_qfq_relabeled_as_none",
                "poisoned_rows": poisoned,
                "deleted_bars": bars_deleted,
                "deleted_ranges": ranges_deleted,
                "threshold": POISONED_NONE_DAILY_CLOSE_THRESHOLD,
                "hint": (
                    "Re-run backtest; cache will refill from upstream with "
                    f"adjust={DEFAULT_BAR_ADJUST} by default, or with the caller's explicit adjust mode."
                ),
            },
        )
        return {
            "poisoned_rows": poisoned,
            "deleted_bars": bars_deleted,
            "deleted_ranges": ranges_deleted,
        }
