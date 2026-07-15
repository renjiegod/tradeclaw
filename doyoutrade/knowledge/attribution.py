"""交割单归因 (broker-statement attribution) — FIFO round-trip P&L over ``trades/``.

The user keeps their broker-exported 交割单 (execution statements) as raw CSV
under the private KB partition ``trades/`` (skill naming
``trades/<broker>/<YYYY-MM>.csv``, kept verbatim — never hand-edited). This
module is the read-side that turns those raw exports into a structured
attribution feed for the frontend "归因看板" (attribution dashboard):

1. **Column-alias normalisation** (:func:`_resolve_columns`) — different brokers
   (华泰 / 国君 / 银河 / 东财 / 中信 …) export different column names for the
   same field. A tolerant alias resolver maps them onto the canonical fields
   ``date / time / symbol / name / side / price / qty / amount`` by header
   substring match (case- and full/half-width-tolerant). A file that cannot map
   the core columns (``date / symbol / side / price / qty``) is NOT guessed at —
   it lands in ``unparsed`` so no wrong P&L is ever produced.
2. **FIFO round-trip pairing** (:func:`_pair_symbol_fills`) — per symbol, sorted
   by ``(date, time, appearance)``, a FIFO buy queue is drained by each sell to
   accumulate *round-trips* (a round-trip = build-to-flat: from the first buy
   after flat, to the sell that returns the position to zero). Money is
   ``Decimal`` throughout, serialised to decimal strings on the way out.
3. **Attribution statistics** (:func:`read_trade_attribution`) — win rate,
   total / avg realised P&L, profit factor, avg hold days, best / worst
   round-trip, per-symbol rollups, and an ``unparsed`` list surfacing every
   file / row that could not be parsed (never silently dropped).

Discipline (AGENTS.md §错误可见性 / §金额纪律):
- All money is computed with :class:`decimal.Decimal` and serialised to
  decimal strings (never floats) via :func:`decimal_to_json_str`.
- A CSV whose core columns cannot be mapped is recorded in ``unparsed`` with a
  reason — the module never guesses a column layout and never emits bogus P&L.
- Bad rows are skipped **loudly** (``logger.info`` / ``logger.warning`` with the
  reason) and counted into ``unparsed`` — not swallowed with a silent ``pass``.
- A sell with no matching buy (卖超买) is surfaced as an ``orphan_sell`` skip —
  it never creates a phantom negative position or a fabricated round-trip.
- An un-flattened tail position (未平仓尾单) is NOT counted as realised P&L; it
  is only reflected in ``summary.open_positions`` (a count of still-open
  symbols).
- The library never calls :func:`datetime.now` — every date comes from the CSV.
- Reads resolve through ``knowledge_root()`` (honours ``DOYOUTRADE_HOME``); the
  ``trades/`` partition is private KB memory and never leaves this surface.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str
from doyoutrade.tools._sandbox import knowledge_root

logger = logging.getLogger(__name__)

#: The canonical normalised fields. ``date / symbol / side / price / qty`` are
#: the CORE columns — a file that cannot map all five is unparseable and lands
#: in ``unparsed`` rather than producing wrong P&L. ``time / name / amount`` are
#: optional (amount falls back to ``price * qty`` when absent).
_CORE_FIELDS: tuple[str, ...] = ("date", "symbol", "side", "price", "qty")

#: Column-name aliases per canonical field. Matching is by **substring** against
#: a normalised (lower-cased, full-width-folded, whitespace-stripped) header, so
#: e.g. a header ``"成交日期 "`` or ``"发生日期"`` both map to ``date``. Aliases
#: are ordered longest-first within each field so a more specific alias wins the
#: header (e.g. ``成交金额`` over ``金额``) — see :func:`_resolve_columns`.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    # date first — 成交日期 / 交易日期 / 发生日期 / 日期 …
    "date": ("成交日期", "交易日期", "发生日期", "清算日期", "日期", "date"),
    # time — 成交时间 / 时间 (optional)
    "time": ("成交时间", "委托时间", "时间", "time"),
    # symbol — 证券代码 / 股票代码 / 证券编码 / 代码 …
    "symbol": (
        "证券代码",
        "股票代码",
        "证券编码",
        "标的代码",
        "代码",
        "symbol",
        "code",
    ),
    # name — 证券名称 / 股票名称 / 名称 (optional)
    "name": ("证券名称", "股票名称", "证券简称", "名称", "name"),
    # side — 买卖标志 / 买卖方向 / 委托类别 / 操作 …
    "side": (
        "买卖标志",
        "买卖方向",
        "委托类别",
        "业务名称",
        "操作",
        "方向",
        "买卖",
        "side",
    ),
    # price — 成交价格 / 成交均价 / 价格 …
    "price": ("成交均价", "成交价格", "成交价", "均价", "价格", "price"),
    # qty — 成交数量 / 成交股数 / 成交量 / 数量 …
    "qty": ("成交数量", "成交股数", "成交量", "数量", "股数", "qty", "volume"),
    # amount — 成交金额 / 成交额 / 发生金额 (optional; else price*qty)
    "amount": ("成交金额", "发生金额", "成交额", "金额", "amount"),
}

#: Full-width → half-width digit / punctuation folding table so a header like
#: ``"证券代码"`` with full-width chars, or a value like ``"６００５１９"``,
#: normalises to the ASCII form before matching / parsing.
_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # full-width space → ASCII space


def _fold(text: str) -> str:
    """Normalise a header / value for tolerant matching.

    Folds full-width ASCII to half-width, lower-cases, and strips surrounding
    whitespace + a leading UTF-8 BOM (broker exports often ship one on the first
    header cell). Kept deliberately simple — enough to bridge the常见 broker
    format差异 without pretending to be a full unicode normaliser.
    """
    s = str(text or "").translate(_FULLWIDTH_MAP)
    s = s.lstrip("﻿").strip().lower()
    return s


def _resolve_columns(columns: list[str]) -> dict[str, int]:
    """Map canonical fields → column index by tolerant substring matching.

    For each header (in order), find the canonical field whose alias list has
    the **longest** alias contained in the folded header, and assign that header
    to the field if the field is not already assigned (first header wins a
    field, so a leftmost ``成交日期`` column beats a later ``清算日期`` one).

    Returns a dict of ``{canonical_field: column_index}`` containing only the
    fields that matched. The caller checks the CORE subset is present.
    """
    folded = [_fold(c) for c in columns]
    resolved: dict[str, int] = {}
    for idx, header in enumerate(folded):
        if not header:
            continue
        # Find the best (longest-alias) canonical field this header matches.
        best_field: str | None = None
        best_len = 0
        for field, aliases in _COLUMN_ALIASES.items():
            if field in resolved:
                continue  # field already claimed by an earlier column
            for alias in aliases:
                if alias in header and len(alias) > best_len:
                    best_field = field
                    best_len = len(alias)
        if best_field is not None:
            resolved[best_field] = idx
    return resolved


def _classify_side(side: Any) -> str:
    """Normalise a broker side value to ``buy`` / ``sell`` / ``""``.

    Mirrors :func:`doyoutrade.assistant.review_analytics._classify_side` but
    matches by **substring** (broker 买卖标志 values are often verbose, e.g.
    "证券买入" / "买入" / "担保品卖出"), so anything containing 买/buy/b →
    ``buy``, 卖/sell/s → ``sell``. Non-trade rows (申购 / 红利 / 费用 / 利息 …)
    contain neither and map to ``""`` so they are skipped, not mis-bucketed.
    """
    s = _fold(side)
    if not s:
        return ""
    if "买" in s or "buy" in s or s == "b":
        return "buy"
    if "卖" in s or "sell" in s or s == "s":
        return "sell"
    return ""


def _normalise_symbol(raw: str) -> str:
    """Best-effort canonical symbol; always keep something non-empty.

    Strips whitespace / BOM and folds full-width digits. If the token is a bare
    6-digit code we keep it as-is (the raw code is preserved either way); we do
    NOT invent an exchange suffix — the raw code is enough to group fills, and
    guessing SH/SZ would be a silent fabrication. Any non-empty original is kept
    verbatim when folding leaves nothing recognisable.
    """
    folded = str(raw or "").translate(_FULLWIDTH_MAP).lstrip("﻿").strip()
    return folded


def _parse_decimal(value: Any) -> Decimal | None:
    """Parse a money / quantity cell to ``Decimal``; ``None`` on failure.

    Tolerant of thousands separators and stray currency symbols
    (``"1,800.00"`` / ``"¥1800"``) but returns ``None`` (never 0) when the cell
    is empty or unparseable — a missing price/qty makes the row unusable, and
    coercing it to 0 would silently fabricate a free trade (§错误可见性).
    """
    if value is None:
        return None
    s = str(value).translate(_FULLWIDTH_MAP).strip()
    if not s:
        return None
    # Drop common non-numeric decorations brokers sometimes leave in cells.
    s = s.replace(",", "").replace("¥", "").replace("￥", "").replace("$", "")
    if not s:
        return None
    try:
        return decimal_from_number(s)
    except (InvalidOperation, ValueError):
        return None


def _month_of(iso_date: str) -> str | None:
    """``YYYY-MM`` of an ISO date string, or ``None`` if unparseable."""
    try:
        d = date.fromisoformat(iso_date)
    except (TypeError, ValueError):
        return None
    return f"{d.year:04d}-{d.month:02d}"


def _normalise_date(raw: str) -> str | None:
    """Normalise a broker date cell to ISO ``YYYY-MM-DD``; ``None`` on failure.

    Accepts ``2026-07-03`` / ``2026/07/03`` / ``20260703`` (the three shapes
    seen across broker exports). Returns ``None`` for anything else so the row
    is surfaced as unparseable rather than silently mis-dated.
    """
    s = str(raw or "").translate(_FULLWIDTH_MAP).strip()
    if not s:
        return None
    s = s.replace("/", "-").replace(".", "-")
    # Compact 8-digit form → insert dashes.
    if len(s) == 8 and s.isdigit():
        s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


class _Fill:
    """A single normalised buy/sell fill (internal to the pairing pass)."""

    __slots__ = ("date", "time", "symbol", "name", "side", "price", "qty", "amount", "order")

    def __init__(
        self,
        *,
        date: str,
        time: str,
        symbol: str,
        name: str | None,
        side: str,
        price: Decimal,
        qty: Decimal,
        amount: Decimal,
        order: int,
    ) -> None:
        self.date = date
        self.time = time
        self.symbol = symbol
        self.name = name
        self.side = side
        self.price = price
        self.qty = qty
        self.amount = amount
        self.order = order


def _parse_file(path: Path, root: Path) -> tuple[list[_Fill], list[dict[str, str]]]:
    """Parse one broker CSV into normalised fills + a list of unparsed reasons.

    Returns ``(fills, unparsed)``. When the file's core columns cannot be mapped
    the fills list is empty and ``unparsed`` carries a single file-level entry.
    Per-row failures (bad side / unparseable price / qty / date) are appended as
    row-level ``unparsed`` entries and skipped loudly.
    """
    fills: list[_Fill] = []
    unparsed: list[dict[str, str]] = []
    rel = _safe_rel(path, root)

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning(
            "trade_attribution: read failed %s (%s): %s", path, type(exc).__name__, exc
        )
        unparsed.append(
            {
                "path": rel,
                "reason": "read_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "hint": "broker CSV unreadable; check encoding / file permissions",
            }
        )
        return fills, unparsed

    records = list(csv.reader(text.splitlines()))
    if not records:
        unparsed.append(
            {"path": rel, "reason": "empty_file", "hint": "no header row in CSV"}
        )
        return fills, unparsed

    columns = records[0]
    resolved = _resolve_columns(columns)
    missing = [f for f in _CORE_FIELDS if f not in resolved]
    if missing:
        logger.info(
            "trade_attribution: file %s missing core columns %s (headers=%r); "
            "recorded as unparsed",
            rel, missing, columns,
        )
        unparsed.append(
            {
                "path": rel,
                "reason": "core_columns_unmapped",
                "detail": "missing: " + ", ".join(missing),
                "hint": "header names not recognised; extend _COLUMN_ALIASES or "
                "keep the broker's original header row",
            }
        )
        return fills, unparsed

    order = 0
    for line_no, row in enumerate(records[1:], start=2):
        if not row or all(not str(c).strip() for c in row):
            continue  # blank spacer line — not an error, silently skip
        # Guard against short rows (a trailing summary line, ragged export).
        max_idx = max(resolved.values())
        if len(row) <= max_idx:
            unparsed.append(
                {
                    "path": rel,
                    "reason": "short_row",
                    "detail": f"line {line_no}: {len(row)} cols < needed {max_idx + 1}",
                    "hint": "row has fewer columns than the header; likely a "
                    "footer/summary line",
                }
            )
            logger.info(
                "trade_attribution: %s line %d short row (%d cols); skipped",
                rel, line_no, len(row),
            )
            continue

        side = _classify_side(row[resolved["side"]])
        if side not in ("buy", "sell"):
            # 申购 / 红利 / 费用 / 利息 … — a non-trade row, not an error. Record
            # it as a benign skip so the operator can see nothing was lost.
            unparsed.append(
                {
                    "path": rel,
                    "reason": "non_trade_side",
                    "detail": f"line {line_no}: side={row[resolved['side']]!r}",
                    "hint": "row is not a buy/sell (dividend/subscription/fee); "
                    "excluded from round-trip pairing",
                }
            )
            continue

        iso_date = _normalise_date(row[resolved["date"]])
        symbol = _normalise_symbol(row[resolved["symbol"]])
        price = _parse_decimal(row[resolved["price"]])
        qty = _parse_decimal(row[resolved["qty"]])

        bad: list[str] = []
        if iso_date is None:
            bad.append(f"date={row[resolved['date']]!r}")
        if not symbol:
            bad.append("symbol=<empty>")
        if price is None:
            bad.append(f"price={row[resolved['price']]!r}")
        if qty is None or qty <= 0:
            bad.append(f"qty={row[resolved['qty']]!r}")
        if bad:
            unparsed.append(
                {
                    "path": rel,
                    "reason": "bad_row_values",
                    "detail": f"line {line_no}: " + ", ".join(bad),
                    "hint": "unparseable core cell(s); row excluded from pairing",
                }
            )
            logger.info(
                "trade_attribution: %s line %d bad values (%s); skipped",
                rel, line_no, ", ".join(bad),
            )
            continue

        # amount: prefer the explicit column, else price*qty (both Decimal).
        amount: Decimal | None = None
        if "amount" in resolved:
            amount = _parse_decimal(row[resolved["amount"]])
        if amount is None:
            amount = price * qty

        time_val = ""
        if "time" in resolved:
            time_val = str(row[resolved["time"]] or "").strip()
        name_val: str | None = None
        if "name" in resolved:
            name_val = str(row[resolved["name"]] or "").strip() or None

        fills.append(
            _Fill(
                date=iso_date,
                time=time_val,
                symbol=symbol,
                name=name_val,
                side=side,
                price=price,
                qty=qty,
                amount=amount,
                order=order,
            )
        )
        order += 1

    return fills, unparsed


def _safe_rel(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` as posix, falling back to the name."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _pair_symbol_fills(
    symbol: str,
    fills: list[_Fill],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], Decimal]:
    """FIFO-pair one symbol's fills into round-trips.

    Fills MUST already be sorted by ``(date, time, order)``. Maintains a FIFO
    queue of open buy lots (each ``[remaining_qty, price, open_date]``). Each
    sell drains the queue front-to-back, accumulating the current round-trip;
    when the position returns to zero the round-trip is finalised.

    Returns ``(round_trips, unparsed, open_qty)`` where:
      - ``round_trips`` — finalised (flat) round-trip dicts (money as Decimal
        strings, see :func:`_finalise_round_trip`).
      - ``unparsed`` — ``orphan_sell`` skips (a sell with no matching buy).
      - ``open_qty`` — remaining un-flattened buy quantity (>0 ⇒ still-open
        position; drives ``summary.open_positions``). NOT realised P&L.
    """
    round_trips: list[dict[str, Any]] = []
    unparsed: list[dict[str, str]] = []

    # Open buy lots as [remaining_qty, price, open_date].
    lots: list[list[Any]] = []
    # Accumulators for the current (in-progress) round-trip.
    rt_open_date: str | None = None
    rt_close_date: str | None = None
    rt_qty = Decimal(0)
    rt_buy_cost = Decimal(0)  # Σ matched buy qty * buy price
    rt_sell_proceeds = Decimal(0)  # Σ matched sell qty * sell price
    name: str | None = None

    def _reset_rt() -> None:
        nonlocal rt_open_date, rt_close_date, rt_qty, rt_buy_cost, rt_sell_proceeds
        rt_open_date = None
        rt_close_date = None
        rt_qty = Decimal(0)
        rt_buy_cost = Decimal(0)
        rt_sell_proceeds = Decimal(0)

    for fill in fills:
        if fill.name and not name:
            name = fill.name
        if fill.side == "buy":
            lots.append([fill.qty, fill.price, fill.date])
            continue

        # --- sell: drain the FIFO buy queue ---
        sell_remaining = fill.qty
        while sell_remaining > 0 and lots:
            lot = lots[0]
            lot_qty, lot_price, lot_open = lot[0], lot[1], lot[2]
            matched = min(sell_remaining, lot_qty)

            if rt_open_date is None:
                rt_open_date = lot_open  # first buy of this round-trip
            rt_qty += matched
            rt_buy_cost += matched * lot_price
            rt_sell_proceeds += matched * fill.price
            rt_close_date = fill.date

            lot[0] = lot_qty - matched
            sell_remaining -= matched
            if lot[0] <= 0:
                lots.pop(0)

            # Position flat → finalise this round-trip.
            if not lots:
                round_trips.append(
                    _finalise_round_trip(
                        symbol=symbol,
                        name=name,
                        open_date=rt_open_date,
                        close_date=rt_close_date,
                        qty=rt_qty,
                        buy_cost=rt_buy_cost,
                        sell_proceeds=rt_sell_proceeds,
                    )
                )
                _reset_rt()

        if sell_remaining > 0:
            # Sell with no matching buy (卖超买). Surface it as an orphan skip —
            # never fabricate a negative position or a phantom round-trip.
            unparsed.append(
                {
                    "path": f"symbol:{symbol}",
                    "reason": "orphan_sell",
                    "detail": (
                        f"{fill.date} sold {sell_remaining} more than held "
                        f"(no matching buy)"
                    ),
                    "hint": "sell exceeds prior buys; check for missing early "
                    "buy rows or an opening position not in this export",
                }
            )
            logger.info(
                "trade_attribution: symbol=%s orphan sell date=%s excess_qty=%s; "
                "skipped (no phantom position)",
                symbol, fill.date, sell_remaining,
            )

    open_qty = sum((lot[0] for lot in lots), Decimal(0))
    return round_trips, unparsed, open_qty


def _finalise_round_trip(
    *,
    symbol: str,
    name: str | None,
    open_date: str | None,
    close_date: str | None,
    qty: Decimal,
    buy_cost: Decimal,
    sell_proceeds: Decimal,
) -> dict[str, Any]:
    """Build one round-trip result dict (money as decimal strings).

    ``realized_pnl = sell_proceeds - buy_cost`` (gross of fees — the raw export
    is what the user has; fees, when present, are not netted here to keep the
    number reproducible against the CSV). ``return_pct`` is P&L / buy_cost ×100
    (``None`` when buy_cost is 0, which cannot normally happen for a paired
    round-trip but is guarded rather than dividing by zero). ``hold_days`` is the
    calendar-day gap open→close (``None`` if either date is missing).
    """
    realized = sell_proceeds - buy_cost
    avg_buy = buy_cost / qty if qty > 0 else Decimal(0)
    avg_sell = sell_proceeds / qty if qty > 0 else Decimal(0)
    return_pct: float | None
    if buy_cost > 0:
        return_pct = float(realized / buy_cost * Decimal(100))
    else:
        return_pct = None

    hold_days: int | None = None
    if open_date and close_date:
        try:
            hold_days = (date.fromisoformat(close_date) - date.fromisoformat(open_date)).days
        except (TypeError, ValueError):
            hold_days = None

    return {
        "symbol": symbol,
        "name": name,
        "open_date": open_date,
        "close_date": close_date,
        "qty": decimal_to_json_str(qty),
        "avg_buy": decimal_to_json_str(avg_buy),
        "avg_sell": decimal_to_json_str(avg_sell),
        "buy_cost": decimal_to_json_str(buy_cost),
        "sell_proceeds": decimal_to_json_str(sell_proceeds),
        "realized_pnl": decimal_to_json_str(realized),
        "return_pct": round(return_pct, 4) if return_pct is not None else None,
        "hold_days": hold_days,
    }


def _iter_trade_csvs(root: Path) -> list[Path]:
    """All ``trades/**/*.csv`` under the KB root, sorted for deterministic order."""
    trades_root = root / "trades"
    if not trades_root.is_dir():
        return []
    return sorted(p for p in trades_root.rglob("*.csv") if p.is_file())


def _within_months(close_date: str | None, cutoff: date | None) -> bool:
    """Keep a round-trip whose ``close_date`` is on/after ``cutoff`` (or no cutoff)."""
    if cutoff is None:
        return True
    if not close_date:
        return False
    try:
        d = date.fromisoformat(close_date)
    except (TypeError, ValueError):
        return False
    return d >= cutoff


def _compute_summary(round_trips: list[dict[str, Any]], open_positions: int) -> dict[str, Any]:
    """Aggregate finalised round-trips into the summary block.

    Money fields are decimal strings; ratios are floats; counts are ints.
    Empty input yields zero-valued counts and ``None`` for averages / best /
    worst / rates (never a fabricated 0 — an empty book is "unknown", not "0%
    win rate").
    """
    n = len(round_trips)
    if n == 0:
        return {
            "round_trips": 0,
            "win_count": 0,
            "loss_count": 0,
            "flat_count": 0,
            "win_rate": None,
            "total_realized_pnl": "0",
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "avg_hold_days": None,
            "best": None,
            "worst": None,
            "open_positions": open_positions,
        }

    total = Decimal(0)
    gross_win = Decimal(0)
    gross_loss = Decimal(0)  # absolute value of losses
    win_count = 0
    loss_count = 0
    flat_count = 0
    hold_days_vals: list[int] = []
    best = round_trips[0]
    worst = round_trips[0]
    best_pnl = decimal_from_number(best["realized_pnl"])
    worst_pnl = best_pnl

    for rt in round_trips:
        pnl = decimal_from_number(rt["realized_pnl"])
        total += pnl
        if pnl > 0:
            win_count += 1
            gross_win += pnl
        elif pnl < 0:
            loss_count += 1
            gross_loss += -pnl
        else:
            flat_count += 1
        hd = rt.get("hold_days")
        if isinstance(hd, int):
            hold_days_vals.append(hd)
        if pnl > best_pnl:
            best_pnl = pnl
            best = rt
        if pnl < worst_pnl:
            worst_pnl = pnl
            worst = rt

    # win_rate over decided (win+loss) round-trips; flat trades don't count
    # either way (they neither won nor lost).
    decided = win_count + loss_count
    win_rate = round(win_count / decided, 4) if decided > 0 else None
    avg_win = decimal_to_json_str(gross_win / win_count) if win_count else None
    avg_loss = decimal_to_json_str(-gross_loss / loss_count) if loss_count else None
    # profit_factor = gross wins / gross losses (abs). None when no losses (an
    # all-winning book has no finite factor — surface None, don't fabricate ∞).
    if gross_loss > 0:
        profit_factor = round(float(gross_win / gross_loss), 4)
    else:
        profit_factor = None
    avg_hold_days = (
        round(sum(hold_days_vals) / len(hold_days_vals), 2) if hold_days_vals else None
    )

    def _extreme(rt: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": rt["symbol"],
            "name": rt.get("name"),
            "realized_pnl": rt["realized_pnl"],
            "return_pct": rt.get("return_pct"),
            "close_date": rt.get("close_date"),
        }

    return {
        "round_trips": n,
        "win_count": win_count,
        "loss_count": loss_count,
        "flat_count": flat_count,
        "win_rate": win_rate,
        "total_realized_pnl": decimal_to_json_str(total),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_days": avg_hold_days,
        "best": _extreme(best),
        "worst": _extreme(worst),
        "open_positions": open_positions,
    }


def _compute_by_symbol(round_trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-symbol rollups: round-trip count, realised P&L, win rate.

    Sorted by realised P&L descending (biggest winners first). Money is decimal
    strings; win_rate is over decided round-trips per symbol (None when a symbol
    has only flat trades).
    """
    by: dict[str, dict[str, Any]] = {}
    for rt in round_trips:
        sym = rt["symbol"]
        acc = by.setdefault(
            sym,
            {
                "symbol": sym,
                "name": rt.get("name"),
                "round_trips": 0,
                "_pnl": Decimal(0),
                "_win": 0,
                "_loss": 0,
            },
        )
        if not acc.get("name") and rt.get("name"):
            acc["name"] = rt.get("name")
        acc["round_trips"] += 1
        pnl = decimal_from_number(rt["realized_pnl"])
        acc["_pnl"] += pnl
        if pnl > 0:
            acc["_win"] += 1
        elif pnl < 0:
            acc["_loss"] += 1

    out: list[dict[str, Any]] = []
    for acc in by.values():
        decided = acc["_win"] + acc["_loss"]
        out.append(
            {
                "symbol": acc["symbol"],
                "name": acc["name"],
                "round_trips": acc["round_trips"],
                "realized_pnl": decimal_to_json_str(acc["_pnl"]),
                "win_rate": round(acc["_win"] / decided, 4) if decided > 0 else None,
            }
        )
    out.sort(key=lambda r: decimal_from_number(r["realized_pnl"]), reverse=True)
    return out


def read_trade_attribution(
    *,
    months: int | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Read ``trades/**/*.csv``, FIFO-pair round-trips, and compute attribution.

    Walks every broker CSV under ``<root>/trades/``, normalises columns (tolerant
    of per-broker header differences), FIFO-pairs each symbol's fills into
    round-trips, and rolls up win rate / realised P&L / profit factor / hold days
    / best-worst / per-symbol stats. Every file / row that could not be parsed is
    surfaced in ``unparsed`` — nothing is silently dropped.

    ``months`` (optional) keeps only round-trips whose ``close_date`` falls in
    the most recent ``months`` calendar months relative to the newest recorded
    close date (self-relative window, so it works regardless of "today" and the
    library never calls ``datetime.now``). ``None`` (default) returns all.

    ``root`` defaults to ``knowledge_root()``; the ``/knowledge`` API router
    passes its own resolver so this read is anchored to the same KB base as the
    rest of that surface. A fresh KB / absent ``trades/`` returns a structured
    empty result (zeroed summary, empty lists), not an error.

    Returns ``{summary, round_trips, by_symbol, unparsed}``:
      - ``summary`` — see :func:`_compute_summary`.
      - ``round_trips`` — finalised round-trip detail, ``close_date`` descending.
      - ``by_symbol`` — per-symbol rollups, realised P&L descending.
      - ``unparsed`` — file/row-level parse failures + skips, with reasons.
    """
    if root is None:
        root = knowledge_root()
    root = root.expanduser()

    all_round_trips: list[dict[str, Any]] = []
    unparsed: list[dict[str, str]] = []
    open_positions_symbols: set[str] = set()

    # Group fills by symbol across ALL files (a symbol's history can span
    # multiple monthly exports; pairing must see the whole timeline).
    fills_by_symbol: dict[str, list[_Fill]] = {}
    for path in _iter_trade_csvs(root):
        file_fills, file_unparsed = _parse_file(path, root)
        unparsed.extend(file_unparsed)
        for fill in file_fills:
            fills_by_symbol.setdefault(fill.symbol, []).append(fill)

    for symbol in sorted(fills_by_symbol):
        fills = fills_by_symbol[symbol]
        # Deterministic chronological order: (date, time, appearance order).
        fills.sort(key=lambda f: (f.date, f.time, f.order))
        rts, sym_unparsed, open_qty = _pair_symbol_fills(symbol, fills)
        all_round_trips.extend(rts)
        unparsed.extend(sym_unparsed)
        if open_qty > 0:
            open_positions_symbols.add(symbol)

    # months window (self-relative to the newest close date).
    cutoff: date | None = None
    if months is not None and months >= 1 and all_round_trips:
        closes = [
            date.fromisoformat(rt["close_date"])
            for rt in all_round_trips
            if rt.get("close_date")
        ]
        if closes:
            newest = max(closes)
            total_month = newest.year * 12 + (newest.month - 1)
            cutoff_month = total_month - (months - 1)
            cutoff_year, cutoff_mon = divmod(cutoff_month, 12)
            cutoff = date(cutoff_year, cutoff_mon + 1, 1)

    if cutoff is not None:
        all_round_trips = [
            rt for rt in all_round_trips if _within_months(rt.get("close_date"), cutoff)
        ]

    all_round_trips.sort(key=lambda rt: str(rt.get("close_date") or ""), reverse=True)

    summary = _compute_summary(all_round_trips, len(open_positions_symbols))
    by_symbol = _compute_by_symbol(all_round_trips)

    return {
        "summary": summary,
        "round_trips": all_round_trips,
        "by_symbol": by_symbol,
        "unparsed": unparsed,
    }


__all__ = ["read_trade_attribution"]
