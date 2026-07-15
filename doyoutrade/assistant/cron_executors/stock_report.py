"""``stock_report`` cron task executor — 模板化个股研报.

Use case: the user asks for a recurring per-symbol report ("每天收盘后给我推
这几只票的研报"). At fire time this executor gathers recent daily bars per
symbol, builds :class:`~doyoutrade.assistant.reporting.ReportItem` rows with a
**deterministic rule layer** (no LLM call — close vs MA20, RSI14, 5-day
change), renders Markdown through the report templates, optionally converts it
to a PNG (``rendering.md2img``), delivers it to the target session (image via
the session's bound channel, text via the shared delivery primitive), and
persists the Markdown to the private KB under ``reports/``.

Fire-time pipeline (mirrors :class:`DailyReviewExecutor` for span / debug
event / TaskResult shape):

  1. Per symbol: fetch ~60 recent daily bars via the injected
     ``bars_provider`` (lazy default: ``build_trading_data_stack``). A single
     symbol failure emits ``stock_report.symbol_failed`` and is skipped; only
     when **every** symbol fails does the task return ``status='failed'``.
  2. ``stock_report.gathered`` — how many symbols succeeded / failed.
  3. ``render_report(...)`` → ``stock_report.rendered``.
  4. ``as_image=True``: ``render_markdown_to_image``; a render failure emits
     ``md2img_unavailable`` (``MD2IMG_UNAVAILABLE_EVENT``) and falls back to
     text — never fails the fire.
  5. Delivery: image goes through the session's bound channel
     (``channel.send(ImageContent)``); a send failure emits
     ``stock_report.image_delivery_failed`` and falls back to text via
     :func:`deliver_assistant_message_to_session`. ``stock_report.delivered``
     records the final mode + status.
  6. KB write-back to ``reports/<YYYY>/<YYYY-MM-DD>-<slug>.md`` (sandboxed,
     same pattern as ``knowledge/review.py``); failure is non-fatal and emits
     ``stock_report.journal_failed``.

Params (validated by :meth:`validate_params`):

  - ``symbols`` (list[str], required, non-empty) — canonical symbols.
  - ``title`` (str | None) — report title; defaults per language.
  - ``language`` ("zh" | "en", default "zh").
  - ``as_image`` (bool, default False) — push a PNG when possible.
  - ``target_session_id`` (str | None) — session to push into (null →
    ``delivery_status='skipped'``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, ClassVar, Optional
from zoneinfo import ZoneInfo

from doyoutrade.assistant.rendering.md2img import (
    MD2IMG_UNAVAILABLE_EVENT,
    render_markdown_to_image,
)
from doyoutrade.assistant.reporting import ReportItem, ReportRequest, render_report
from doyoutrade.debug import emit_debug_event
from doyoutrade.observability import get_logger, get_tracer

from ._deliver import deliver_assistant_message_to_session
from .base import JobRunContext, TaskResult

logger = get_logger(__name__)
tracer = get_tracer(__name__)

KIND = "stock_report"

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")

#: ``bars_provider(symbol, start_iso, end_iso) -> list[Bar]`` — one symbol's
#: recent daily bars (``doyoutrade.core.models.Bar``-shaped: ``.close`` floats).
BarsProvider = Callable[[str, str, str], Awaitable[list[Any]]]

#: Daily bars requested per symbol (rule layer needs >= _MIN_BARS).
_BARS_WANTED = 60
#: Calendar-day lookback that comfortably yields ``_BARS_WANTED`` trading bars.
_LOOKBACK_DAYS = 120
#: Minimum bars required to score a symbol (MA20 + last-day change).
_MIN_BARS = 21

_SUPPORTED_LANGUAGES = ("zh", "en")


def _asof_from_fired_at(fired_at: datetime) -> date:
    """The trading day a fire belongs to, in Asia/Shanghai (A-share local)."""
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    return fired_at.astimezone(_A_SHARE_TZ).date()


def _slugify(title: str) -> str:
    """Filesystem-safe slug from a (possibly CJK) title; never empty."""
    out: list[str] = []
    for ch in title.strip().lower():
        if ch.isascii() and (ch.isalnum()):
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "stock-report"


def _rsi14(closes: list[float]) -> Optional[float]:
    """Wilder's RSI(14) on a plain close list; None when history is short."""
    period = 14
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def build_report_item(symbol: str, bars: list[Any], *, language: str) -> ReportItem:
    """Deterministic rule layer: bars → scored :class:`ReportItem` (no LLM).

    Rules (documented so the score is auditable):
      - base 50
      - close > MA20 → +15, else -15
      - 5-day change > 0 → +10, else -10
      - RSI14 < 30 (oversold) → +10; RSI14 > 70 (overbought) → -10
      - clamp to [0, 100]; action: >=65 buy / <=35 sell / else watch

    Raises ``ValueError`` when ``bars`` is too short to score — the caller
    treats that as a per-symbol gather failure, not a silent placeholder row.
    """
    closes = [float(b.close) for b in bars]
    if len(closes) < _MIN_BARS:
        raise ValueError(
            f"insufficient bars for {symbol}: got {len(closes)}, need >= {_MIN_BARS}"
        )
    zh = language == "zh"
    last = closes[-1]
    prev = closes[-2]
    ma20 = sum(closes[-20:]) / 20.0
    change_1d = (last / prev - 1.0) * 100.0 if prev else 0.0
    change_5d = (last / closes[-6] - 1.0) * 100.0 if closes[-6] else 0.0
    rsi = _rsi14(closes)

    score = 50.0
    above_ma20 = last > ma20
    score += 15.0 if above_ma20 else -15.0
    score += 10.0 if change_5d > 0 else -10.0
    if rsi is not None:
        if rsi < 30:
            score += 10.0
        elif rsi > 70:
            score -= 10.0
    score = max(0.0, min(100.0, score))
    action = "buy" if score >= 65 else ("sell" if score <= 35 else "watch")

    if zh:
        trend = "MA20 上方" if above_ma20 else "MA20 下方"
        conclusion = (
            f"收盘 {last:.2f}，位于 {trend}；近 5 日{'涨' if change_5d >= 0 else '跌'} "
            f"{abs(change_5d):.2f}%"
            + (f"，RSI14 {rsi:.1f}" if rsi is not None else "")
            + f"。规则评分 {score:.0f} → {action}。"
        )
    else:
        trend = "above MA20" if above_ma20 else "below MA20"
        conclusion = (
            f"Close {last:.2f}, {trend}; 5-day change {change_5d:+.2f}%"
            + (f", RSI14 {rsi:.1f}" if rsi is not None else "")
            + f". Rule score {score:.0f} -> {action}."
        )

    key_indicators: dict[str, Any] = {
        "MA20": f"{ma20:.2f}",
        "5d_change_pct": f"{change_5d:+.2f}%",
    }
    if rsi is not None:
        key_indicators["RSI14"] = f"{rsi:.1f}"

    return ReportItem(
        symbol=symbol,
        action=action,
        score=score,
        price=last,
        change_pct=change_1d,
        trend=trend,
        core_conclusion=conclusion,
        key_indicators=key_indicators,
    )


def _write_report_journal(
    asof: date, title: str, content: str
) -> dict[str, Any]:
    """Persist the report Markdown to ``reports/<YYYY>/<YYYY-MM-DD>-<slug>.md``.

    Same KB sandbox pattern as ``knowledge/review.py::write_daily_review_journal``.
    Never silently overwrites: a repeat fire on the same day appends a
    numbered suffix (``-2``, ``-3``, ...) to the filename.
    """
    from doyoutrade.tools._sandbox import (
        knowledge_root,
        register_knowledge_sandbox,
        resolve_path,
    )

    register_knowledge_sandbox()  # idempotent: ensures KB dir + writable sandbox
    root = knowledge_root()
    slug = _slugify(title)
    base = f"reports/{asof.year:04d}/{asof.isoformat()}-{slug}"
    rel = f"{base}.md"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = 2
    while target.exists():
        rel = f"{base}-{suffix}.md"
        target = root / rel
        suffix += 1
    resolved = resolve_path(str(target))  # sandbox check (raises if outside KB)
    encoded = content.encode("utf-8")
    resolved.write_text(content, encoding="utf-8")
    return {"path": rel, "bytes_written": len(encoded)}


class StockReportExecutor:
    """Task executor for the ``stock_report`` kind."""

    kind: ClassVar[str] = KIND

    def __init__(
        self,
        *,
        assistant_service: Any,
        cron_job_repository: Any,
        bars_provider: Optional[BarsProvider] = None,
    ):
        self._svc = assistant_service
        self._cron_repo = cron_job_repository
        # Lazy-defaulted so production wiring stays a no-arg construct while
        # tests inject fakes (mirrors DailyReviewExecutor._ensure_market_providers).
        self._bars_provider = bars_provider

    # --- contract validation ----------------------------------------------

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(params, dict):
            return {
                "error_code": "invalid_task_params",
                "error": "params must be an object",
            }
        symbols = params.get("symbols")
        if (
            not isinstance(symbols, list)
            or not symbols
            or not all(isinstance(s, str) and s.strip() for s in symbols)
        ):
            return {
                "error_code": "invalid_symbols",
                "error": "stock_report.params.symbols must be a non-empty list of strings",
                "field": "symbols",
            }
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            return {
                "error_code": "invalid_title",
                "error": "title must be a string or null",
                "field": "title",
            }
        language = params.get("language")
        if language is not None and language not in _SUPPORTED_LANGUAGES:
            return {
                "error_code": "invalid_language",
                "error": f"language must be one of {list(_SUPPORTED_LANGUAGES)}",
                "field": "language",
            }
        as_image = params.get("as_image")
        if as_image is not None and not isinstance(as_image, bool):
            return {
                "error_code": "invalid_as_image",
                "error": "as_image must be a boolean or null",
                "field": "as_image",
            }
        target = params.get("target_session_id")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            return {
                "error_code": "invalid_target_session_id",
                "error": "target_session_id must be a string or null",
                "field": "target_session_id",
            }
        return None

    # --- bars gathering -----------------------------------------------------

    async def _fetch_with_default_provider(
        self, symbols: list[str], start_iso: str, end_iso: str
    ) -> dict[str, list[Any] | Exception]:
        """Fetch all symbols via a one-shot default provider stack.

        Only used when no ``bars_provider`` was injected. Heavy imports are
        lazy so tests (which always inject) never touch the data factory. Per
        symbol the value is either the bars list or the fetch exception (the
        caller turns exceptions into ``stock_report.symbol_failed`` events —
        this helper never swallows them).
        """
        from doyoutrade.config import get_config
        from doyoutrade.data.account_resolution import resolve_default_market_account
        from doyoutrade.data.factory import build_trading_data_stack

        account = await resolve_default_market_account()
        provider, _universe, _account = build_trading_data_stack(
            "auto", get_config().data, list(symbols), account=account
        )
        del _universe, _account
        results: dict[str, list[Any] | Exception] = {}
        try:
            for symbol in symbols:
                try:
                    results[symbol] = list(
                        await provider.get_bars(
                            symbol, start_iso, end_iso, interval="1d"
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — surfaced by caller
                    results[symbol] = exc
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 — cleanup; non-fatal
                    logger.warning(
                        "stock_report bars provider aclose raised (%s): %s",
                        type(exc).__name__, exc,
                    )
        return results

    async def _gather_items(
        self,
        symbols: list[str],
        *,
        asof: date,
        language: str,
        job_id: str,
    ) -> tuple[list[ReportItem], list[str]]:
        """Fetch bars + build a scored :class:`ReportItem` per symbol.

        Per-symbol failures (fetch raised, empty/short bars) emit a
        ``stock_report.symbol_failed`` debug event + ``logger.warning`` and
        the symbol lands in the returned ``failed_symbols`` list — the report
        proceeds with whatever succeeded.
        """
        start_iso = (asof - timedelta(days=_LOOKBACK_DAYS)).isoformat()
        end_iso = asof.isoformat()

        raw: dict[str, list[Any] | Exception] = {}
        if self._bars_provider is not None:
            for symbol in symbols:
                try:
                    raw[symbol] = list(
                        await self._bars_provider(symbol, start_iso, end_iso)
                    )
                except Exception as exc:  # noqa: BLE001 — surfaced below
                    raw[symbol] = exc
        else:
            raw = await self._fetch_with_default_provider(
                symbols, start_iso, end_iso
            )

        items: list[ReportItem] = []
        failed: list[str] = []
        for symbol in symbols:
            value = raw.get(symbol)
            if isinstance(value, Exception):
                failed.append(symbol)
                logger.warning(
                    "stock_report symbol gather failed job_id=%s symbol=%s (%s): %s",
                    job_id, symbol, type(value).__name__, value,
                )
                await emit_debug_event(
                    "stock_report.symbol_failed",
                    {
                        "job_id": job_id,
                        "symbol": symbol,
                        "asof": asof.isoformat(),
                        "error_type": type(value).__name__,
                        "message": str(value),
                        "hint": "bar fetch raised for this symbol; check the data "
                        "provider / symbol canonical form; other symbols proceed",
                    },
                )
                continue
            bars = value or []
            try:
                item = build_report_item(
                    symbol, bars[-_BARS_WANTED:], language=language
                )
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                failed.append(symbol)
                logger.warning(
                    "stock_report symbol scoring failed job_id=%s symbol=%s "
                    "bars=%d (%s): %s",
                    job_id, symbol, len(bars), type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "stock_report.symbol_failed",
                    {
                        "job_id": job_id,
                        "symbol": symbol,
                        "asof": asof.isoformat(),
                        "bar_count": len(bars),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "bars returned but too short/invalid to score "
                        f"(need >= {_MIN_BARS} daily bars); backfill history",
                    },
                )
                continue
            items.append(item)
        return items, failed

    # --- image delivery ------------------------------------------------------

    async def _deliver_image(
        self,
        *,
        target_session_id: str,
        png: bytes,
        caption: str,
        job_id: str,
    ) -> bool:
        """Push a PNG through the session's bound channel. Returns True on success.

        Any failure (no binding, dead channel, send raised) emits
        ``stock_report.image_delivery_failed`` + a warning and returns False so
        the caller falls back to text delivery — the fire never dies here.
        """
        from doyoutrade.assistant.channels.base import ImageContent

        async def _fail(reason: str, exc: Exception | None = None) -> bool:
            payload: dict[str, Any] = {
                "job_id": job_id,
                "target_session_id": target_session_id,
                "reason": reason,
                "hint": "image push could not reach the channel; the report "
                "falls back to text delivery",
            }
            if exc is not None:
                payload["error_type"] = type(exc).__name__
                payload["message"] = str(exc)
            await emit_debug_event("stock_report.image_delivery_failed", payload)
            return False

        try:
            session = await self._svc.get_session(target_session_id)
        except Exception as exc:  # noqa: BLE001 — surfaced, falls back to text
            logger.warning(
                "stock_report image delivery: session lookup failed "
                "session_id=%s job_id=%s (%s): %s",
                target_session_id, job_id, type(exc).__name__, exc,
            )
            return await _fail("session_lookup_failed", exc)

        channel_block = None
        if isinstance(session, dict):
            raw_channel = (session.get("config") or {}).get("channel")
            if isinstance(raw_channel, dict):
                channel_block = raw_channel
        channel_id = str((channel_block or {}).get("channel_id") or "").strip()
        if not channel_id:
            logger.info(
                "stock_report image delivery skipped reason=no_channel_binding "
                "session_id=%s job_id=%s", target_session_id, job_id,
            )
            return await _fail("no_channel_binding")

        manager = getattr(self._svc, "channel_manager", None)
        channel = manager.get(channel_id) if manager is not None else None
        if channel is None:
            logger.info(
                "stock_report image delivery skipped reason=channel_disabled "
                "channel_id=%s session_id=%s job_id=%s",
                channel_id, target_session_id, job_id,
            )
            return await _fail("channel_disabled")

        raw_meta = (channel_block or {}).get("meta")
        send_meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        try:
            await channel.send(
                target_session_id,
                ImageContent(data=png, caption=caption),
                send_meta,
            )
        except Exception as exc:  # noqa: BLE001 — ChannelSendError et al.
            logger.warning(
                "stock_report image channel send failed channel_id=%s "
                "session_id=%s job_id=%s (%s): %s",
                channel_id, target_session_id, job_id, type(exc).__name__, exc,
            )
            return await _fail("channel_send_failed", exc)
        return True

    # --- runtime ----------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: JobRunContext) -> TaskResult:
        with tracer.start_as_current_span("cron.task.run") as span:
            span.set_attribute("cron.task.kind", self.kind)
            span.set_attribute("cron.job_id", ctx.job_id)
            span.set_attribute("cron.job_run_id", ctx.cron_job_run_id)

            symbols = [str(s).strip() for s in (params.get("symbols") or []) if str(s).strip()]
            language = str(params.get("language") or "zh")
            as_image = bool(params.get("as_image") or False)
            target_session_id = params.get("target_session_id")
            if target_session_id is not None:
                target_session_id = str(target_session_id).strip() or None

            asof = _asof_from_fired_at(ctx.fired_at)
            default_title = (
                f"个股研报 {asof.isoformat()}" if language == "zh"
                else f"Stock Report {asof.isoformat()}"
            )
            title = str(params.get("title") or default_title).strip()

            span.set_attribute("stock_report.symbol_count", len(symbols))
            span.set_attribute("stock_report.asof", asof.isoformat())
            span.set_attribute("stock_report.as_image", as_image)

            job = await self._cron_repo.get_job(ctx.job_id)
            if not job:
                span.set_attribute("cron.task.status", "failed")
                return TaskResult(
                    status="failed", error=f"cron job not found: {ctx.job_id}"
                )

            # 1) Gather bars + rule-score each symbol (per-symbol soft-fail).
            items, failed_symbols = await self._gather_items(
                symbols, asof=asof, language=language, job_id=ctx.job_id
            )
            span.set_attribute("stock_report.gathered_count", len(items))
            span.set_attribute("stock_report.failed_count", len(failed_symbols))

            # 2) Gathered checkpoint (visible even when everything failed).
            await emit_debug_event(
                "stock_report.gathered",
                {
                    "job_id": ctx.job_id,
                    "asof": asof.isoformat(),
                    "symbol_count": len(symbols),
                    "gathered_count": len(items),
                    "failed_count": len(failed_symbols),
                    "failed_symbols": failed_symbols,
                },
            )

            if not items:
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute(
                    "cron.task.error", "all symbols failed to gather"
                )
                return TaskResult(
                    status="failed",
                    error=(
                        "stock_report_gather_failed: all "
                        f"{len(symbols)} symbols failed to gather bars"
                    ),
                    data={
                        "asof": asof.isoformat(),
                        "symbols": symbols,
                        "failed_symbols": failed_symbols,
                    },
                )

            # 3) Deterministic render (no LLM).
            markdown = render_report(
                ReportRequest(items=items, title=title, as_of=asof, language=language)
            )
            span.set_attribute("stock_report.markdown_chars", len(markdown))
            await emit_debug_event(
                "stock_report.rendered",
                {
                    "job_id": ctx.job_id,
                    "asof": asof.isoformat(),
                    "item_count": len(items),
                    "markdown_chars": len(markdown),
                },
            )

            # 4) Optional Markdown → PNG (best-effort, never fatal).
            png: bytes | None = None
            image_ok = False
            if as_image:
                result = await render_markdown_to_image(markdown)
                if result.ok:
                    png = result.image
                else:
                    payload = {"job_id": ctx.job_id, **result.failure_payload()}
                    await emit_debug_event(MD2IMG_UNAVAILABLE_EVENT, payload)
                    logger.warning(
                        "stock_report md2img unavailable job_id=%s reason=%s; "
                        "falling back to text",
                        ctx.job_id, result.reason,
                    )
            span.set_attribute("stock_report.image_rendered", png is not None)

            # 5) Delivery — image first (via bound channel), text otherwise/fallback.
            delivery_status: str = "skipped"
            delivery_error: str | None = None
            delivery_mode = "text"
            if png is not None and target_session_id:
                image_ok = await self._deliver_image(
                    target_session_id=target_session_id,
                    png=png,
                    caption=title,
                    job_id=ctx.job_id,
                )
            if image_ok:
                delivery_status = "delivered"
                delivery_mode = "image"
            else:
                status, info = await deliver_assistant_message_to_session(
                    self._svc,
                    target_session_id=target_session_id,
                    content=markdown,
                    cron_job_id=ctx.job_id,
                    cron_job_run_id=ctx.cron_job_run_id,
                    cron_task_kind=self.kind,
                )
                delivery_status = status
                if status == "failed" and isinstance(info, dict):
                    delivery_error = str(info.get("error") or "")
            span.set_attribute("cron.delivery.status", delivery_status)
            span.set_attribute("stock_report.delivery_mode", delivery_mode)
            await emit_debug_event(
                "stock_report.delivered",
                {
                    "job_id": ctx.job_id,
                    "asof": asof.isoformat(),
                    "mode": delivery_mode,
                    "delivery_status": delivery_status,
                    "target_session_id": target_session_id,
                },
            )

            # 6) KB write-back (non-fatal on failure).
            journal_result: dict[str, Any] | None = None
            journal_error: str | None = None
            try:
                journal_result = _write_report_journal(asof, title, markdown)
            except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                journal_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "stock_report journal write failed job_id=%s asof=%s (%s): %s",
                    ctx.job_id, asof, type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "stock_report.journal_failed",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "could not write reports/<YYYY>/<date>-<slug>.md; "
                        "the report was still delivered but not persisted to the KB",
                    },
                )
            else:
                span.set_attribute(
                    "stock_report.report_path", str(journal_result.get("path"))
                )

            span.set_attribute("cron.task.status", "ok")
            return TaskResult(
                status="ok",
                delivery_status=delivery_status,  # type: ignore[arg-type]
                delivery_error=delivery_error,
                data={
                    "asof": asof.isoformat(),
                    "symbols": symbols,
                    "failed_symbols": failed_symbols,
                    "as_image": as_image,
                    "image_ok": image_ok,
                    "delivery_mode": delivery_mode,
                    "report_path": (journal_result or {}).get("path"),
                    "journal_error": journal_error,
                    "target_session_id": target_session_id,
                },
            )
