"""Akshare-based A-share 筹码分布 (chip distribution) provider.

Wraps akshare's ``stock_cyq_em`` (东方财富 筹码分布) into a per-symbol daily
history: 获利比例 (profit ratio), 平均成本 (avg cost), and the 90%/70% cost-band
concentration the upstream computes from OHLCV + turnover (通达信-style
筹码分布 model). The upstream itself trims to its own recent ~90-day window;
callers ask for the most recent ``days`` rows of that.

Failure-mode discipline (per CLAUDE.md §错误可见性), mirrors ``lhb_akshare.py``:

* A *persistent* upstream failure (all retries exhausted) re-raises the last
  exception so the ``data_chips`` tool can surface a distinct
  ``chip_distribution_fetch_failed`` error_code with the exception type.
* A genuinely *empty* result (ETF / index / delisted name — 筹码分布 is an
  A-share-individual-stock-only signal, akshare just returns nothing for
  everything else) returns ``[]`` — the tool maps that to the distinct
  ``chip_distribution_empty`` (a different failure mode than a fetch error).
* Numeric fields that can't be parsed become ``None``, never an
  ``int(脏值)``/``float(脏值)`` silent truncation to 0.

Both paths are observable: the ``akshare.fetch_chip_distribution`` OTel span +
``data_provider.fetch_chip_distribution`` debug event always fire (carrying
the symbol, requested days, and returned row count), and retries log at
WARNING with the attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import akshare as ak

from doyoutrade.core.models import ChipDistributionRow
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# akshare ``stock_cyq_em`` column names (东方财富 筹码分布):
#   日期,获利比例,平均成本,90成本-低,90成本-高,90集中度,70成本-低,70成本-高,70集中度
_COL_DATE = "日期"
_COL_PROFIT_RATIO = "获利比例"
_COL_AVG_COST = "平均成本"
_COL_COST_90_LOW = "90成本-低"
_COL_COST_90_HIGH = "90成本-高"
_COL_CONC_90 = "90集中度"
_COL_COST_70_LOW = "70成本-低"
_COL_COST_70_HIGH = "70成本-高"
_COL_CONC_70 = "70集中度"

_MAX_ATTEMPTS = 3


def _strip_exchange_suffix(symbol: str) -> str:
    """Drop a ``.SH``/``.SZ``/``.BJ`` suffix — akshare wants the bare 6-digit code.

    Only splits on the FIRST dot so a malformed input isn't silently mangled
    beyond recognition (mirrors ``lhb_akshare._strip_exchange_suffix``).
    """
    return symbol.split(".", 1)[0].strip()


def _ensure_working_mini_racer() -> None:
    """Work around ``py-mini-racer==0.6.0``'s broken default ``MiniRacer`` wiring.

    That release's ``__init__.py`` points the package-level ``MiniRacer`` at
    its own legacy compatibility shim (``py_mini_racer.py_mini_racer.MiniRacer``),
    which calls C symbols (``mr_eval_context`` et al.) that the dylib bundled
    in the *same release* no longer exports — every call raises
    ``AttributeError: ... dlsym(..., mr_eval_context): symbol not found``.
    The package's actively-maintained implementation
    (``py_mini_racer._mini_racer.MiniRacer``) matches the shipped dylib and
    works correctly; this is an upstream packaging inconsistency, not a
    platform/architecture issue (confirmed: both the legacy and current
    classes load the identical dylib file).

    akshare's ``stock_cyq_em`` (and any other akshare function embedding a JS
    calculation) does ``import py_mini_racer; py_mini_racer.MiniRacer()`` —
    retargeting the package-level symbol once, before the first call, fixes
    it for every such caller in this process without repinning the
    transitive dependency in ``pyproject.toml``.

    Idempotent and narrowly scoped: only called lazily from
    :meth:`AkshareChipDistributionProvider._sync_fetch` (a process that never
    fetches chip distribution never pays for the import), and only rewires
    when the currently-installed class is actually the broken legacy shim —
    a future py-mini-racer release that fixes its own wiring is left alone.
    """
    import py_mini_racer

    if py_mini_racer.MiniRacer is py_mini_racer.py_mini_racer.MiniRacer:
        from py_mini_racer._mini_racer import MiniRacer as _WorkingMiniRacer

        logger.info(
            "py_mini_racer.MiniRacer retargeted from the broken py-mini-racer==0.6.0 "
            "legacy shim to _mini_racer.MiniRacer (dylib symbol mismatch workaround; "
            "see chip_distribution_akshare._ensure_working_mini_racer)"
        )
        py_mini_racer.MiniRacer = _WorkingMiniRacer


class AkshareChipDistributionProvider:
    """A-share 筹码分布 (chip distribution) source backed by akshare."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # No interval/adjust axis of its own; keep the shape uniform with
        # other non-bar akshare providers (mirrors AkshareDragonTigerProvider).
        supported_intervals=frozenset(),
        default_adjust="none",
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_chip_distribution(
        self, symbol: str, *, days: int = 1
    ) -> List[ChipDistributionRow]:
        """Return the most recent ``days`` daily 筹码分布 rows, oldest first."""
        with data_span("akshare", "fetch_chip_distribution"):
            rows = await asyncio.to_thread(self._sync_fetch, symbol, days)
        _emit_fetch_chip_distribution_event(symbol, days, len(rows))
        return rows

    def _sync_fetch(self, symbol: str, days: int) -> List[ChipDistributionRow]:
        _ensure_working_mini_racer()
        bare_code = _strip_exchange_suffix(symbol)
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_cyq_em(symbol=bare_code)
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare stock_cyq_em failed %s (attempt %d/%d): %s: %s",
                    symbol, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_cyq_em gave up %s: %s: %s",
                        symbol, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info("akshare stock_cyq_em returned no rows for %s", symbol)
            return []

        tail = df.tail(max(days, 1))
        rows: List[ChipDistributionRow] = []
        for _, row in tail.iterrows():
            rows.append(
                ChipDistributionRow(
                    symbol=symbol,
                    date=_clean_date(row.get(_COL_DATE)),
                    provider=PROVIDER_NAME_AKSHARE,
                    profit_ratio=_to_float(row.get(_COL_PROFIT_RATIO)),
                    avg_cost=_to_float(row.get(_COL_AVG_COST)),
                    cost_90_low=_to_float(row.get(_COL_COST_90_LOW)),
                    cost_90_high=_to_float(row.get(_COL_COST_90_HIGH)),
                    concentration_90=_to_float(row.get(_COL_CONC_90)),
                    cost_70_low=_to_float(row.get(_COL_COST_70_LOW)),
                    cost_70_high=_to_float(row.get(_COL_COST_70_HIGH)),
                    concentration_70=_to_float(row.get(_COL_CONC_70)),
                )
            )
        return rows


def _emit_fetch_chip_distribution_event(symbol: str, days: int, row_count: int) -> None:
    _fire_event(
        "data_provider.fetch_chip_distribution",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_chip_distribution",
            "symbol": symbol,
            "days": days,
            "row_count": row_count,
        },
    )


def _fire_event(event_name: str, payload: dict) -> None:
    """Fire emit_debug_event as a fire-and-forget task from a sync/async context."""
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        # No running event loop; skip.
        pass


def _clean_date(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # pandas NaT stringifies to "NaT"; treat as empty.
    return "" if text.lower() == "nat" else text


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        logger.info(
            "chip_distribution numeric skipped reason=unparseable_float raw=%r", value
        )
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


__all__ = ["AkshareChipDistributionProvider"]
