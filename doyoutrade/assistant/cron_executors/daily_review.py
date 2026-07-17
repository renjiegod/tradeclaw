"""``daily_review`` cron task executor — 每日复盘.

Use case: the user asks for a daily after-close review ("每天收盘后帮我复盘当天
交易"). At fire time this executor PRE-GATHERS the data itself (the composing
agent is compose-only and forbidden from calling tools), then composes a
structured 复盘 in one agent turn, persists it to the private knowledge base's
``journal/`` partition, and pushes it to the user.

Fire-time pipeline (mirrors :class:`AgentChatReplyExecutor` for span / error /
delivery shape):

  1. Resolve ``asof`` (the trading day) from ``ctx.fired_at`` in Asia/Shanghai.
  2. Trading-day gate — when a ``trading_day_checker`` is wired, a non-trading
     day (weekday holiday) emits a structured ``daily_review_skipped`` event and
     returns ``delivery_status='suppressed'`` rather than composing against an
     empty day.
  3. Gather the live account statement (cash/equity + positions + asset + the
     day's 交割单) via the injected ``statement_provider`` and a KB digest
     (prior journal / roles / cycles / broker CSV) via
     :func:`build_daily_review_knowledge_digest`.
  3c. **Soft-gather the whole-market 四维** (大盘/情绪/主线/个股) via akshare
     providers (market breadth + concept-board heat + per-holding 龙虎榜). Every
     dimension is best-effort: a failure emits ``daily_review_market_unavailable``
     and is skipped rather than failing the review (eastmoney rate-limits with
     ``RemoteDisconnected`` around after-close). On breadth success the day's
     情绪 row is idempotently upserted into the KB's monthly 情绪周期 log
     (``cycles/<YYYY-MM>/_sentiment.jsonl``) — a non-fatal write
     (``sentiment_log_write_failed`` on error).
  4. Render ``daily_review_framing.j2`` with the gathered data as ``pre_data``
     (including the ``## 今日市场（大盘/情绪/主线）`` block when market data was
     gathered).
  5. Compose via ``AssistantService.send_message`` (run_id / trace / model
     invocations auto-threaded).
  6. Persist the review to ``journal/<YYYY>/<YYYY-MM-DD>.md``.
  7. Deliver via :func:`deliver_assistant_message_to_session`.

Failure modes are distinguished by event name / status (never one generic
except): ``review_data_unavailable`` (statement gather raised),
``review_compose_failed`` (LLM call raised or produced empty text),
``daily_review_journal_failed`` (write-back raised — non-fatal, the composed
review is still delivered), and ``delivery_status='failed'`` from the delivery
primitive.

When the LLM turn fails or returns empty (``review_compose_failed`` /
``empty_reply``), the executor no longer leaves the user empty-handed: it
builds a Python-composed **fallback journal** from the deterministic
analytics layer (:mod:`doyoutrade.assistant.review_analytics`) — same five
sections as the LLM framing, same ``# <asof> 复盘`` title, plus a trailing
``source: "fallback"`` JSON block. The fire then resolves
``status='ok'`` / ``delivery_status='delivered'`` with
``data['fallback_applied']=True`` so downstream UIs can flag the degraded
run. The original ``review_compose_failed`` debug event is still emitted so
the LLM-side failure stays visible (AGENTS.md §错误可见性).

Params (validated by :meth:`validate_params`):

  - ``agent_id`` (str, required) — the agent that composes the review.
  - ``target_session_id`` (str | None) — session to push into (null →
    ``delivery_status='skipped'``).
  - ``account_id`` (str | None) — which account to snapshot; null → default.
  - ``user_request`` (str | None) — the original user phrase; preserved for
    trace attribution and shown in the framing.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Optional
from zoneinfo import ZoneInfo

from doyoutrade.assistant.prompt_templates import render_daily_review_framing
from doyoutrade.assistant.review_analytics import (
    build_fallback_journal,
    build_review_metrics,
    build_rule_diagnostics,
    parse_trailing_review_json,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.knowledge.review import (
    build_daily_review_knowledge_digest,
    upsert_sentiment_log,
    write_daily_review_journal,
)
from doyoutrade.observability import get_logger, get_tracer

from ._deliver import deliver_assistant_message_to_session
from .base import JobRunContext, TaskResult

logger = get_logger(__name__)
tracer = get_tracer(__name__)

KIND = "daily_review"

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")

#: ``statement_provider(account_id, asof, captured_at) -> statement dict``.
StatementProvider = Callable[
    [Optional[str], date, datetime], Awaitable[dict[str, Any]]
]
#: ``trading_day_checker(asof) -> bool``.
TradingDayChecker = Callable[[date], Awaitable[bool]]

_DEFAULT_USER_REQUEST = "每天收盘后帮我复盘当天交易"


#: How many top concept boards (主线题材) to surface in the market block.
_SECTOR_HEAT_TOP_N = 8
#: Cap on holdings we fan out 龙虎榜 lookups for, so a large book cannot turn
#: the (rate-limited) LHB endpoint into a long serial stall on the fire.
_MAX_HOLDINGS_LHB = 20


def _asof_from_fired_at(fired_at: datetime) -> date:
    """The trading day a fire belongs to, in Asia/Shanghai (A-share local)."""
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    return fired_at.astimezone(_A_SHARE_TZ).date()


def _holdings_symbols(statement: dict[str, Any]) -> list[str]:
    """Extract the canonical symbols the user currently holds from a statement.

    Positions live at ``statement['account']['positions']`` (the
    ``build_post_cycle_account`` shape) — each a dict with a ``symbol``. Returns
    a de-duplicated, order-preserving list; empty when there are no positions or
    the account block is missing (a legitimate flat-cash day, not an error).
    """
    account = statement.get("account")
    if not isinstance(account, dict):
        return []
    positions = account.get("positions")
    if not isinstance(positions, list):
        return []
    seen: dict[str, None] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        sym = pos.get("symbol")
        if isinstance(sym, str) and sym.strip():
            seen.setdefault(sym.strip(), None)
    return list(seen)


def _breadth_to_sentiment_row(breadth: Any) -> dict[str, Any]:
    """Project a ``MarketBreadth`` + rule label into the sentiment-log row shape.

    Reuses the tool layer's ``_classify_sentiment`` rule thresholds so the label
    persisted to the KB matches the ``data_market_breadth`` tool's label for the
    same numbers (single source of truth for the情绪 rule), then keeps only the
    fields the sentiment log / frontend timeline care about.
    """
    from doyoutrade.api.operations.data_market_breadth import _classify_sentiment

    sentiment = _classify_sentiment(
        zt=breadth.limit_up_count,
        dt=breadth.limit_down_count,
        zb=breadth.broken_board_count,
        max_streak=breadth.max_streak,
        broken_rate=breadth.broken_board_rate,
    )
    return {
        "label": sentiment["label"],
        "limit_up_count": breadth.limit_up_count,
        "limit_down_count": breadth.limit_down_count,
        "broken_board_count": breadth.broken_board_count,
        "broken_board_rate": round(breadth.broken_board_rate, 4),
        "max_streak": breadth.max_streak,
    }


class DailyReviewExecutor:
    """Task executor for the ``daily_review`` kind."""

    kind: ClassVar[str] = KIND

    def __init__(
        self,
        *,
        assistant_service: Any,
        cron_job_repository: Any,
        statement_provider: StatementProvider,
        trading_day_checker: Optional[TradingDayChecker] = None,
        market_breadth_provider: Any = None,
        sector_provider: Any = None,
        dragon_tiger_provider: Any = None,
        knowledge_graph_repository: Any = None,
        model_adapter_factory: Any = None,
        instrument_catalog_repository: Any = None,
    ):
        self._svc = assistant_service
        self._cron_repo = cron_job_repository
        self._statement_provider = statement_provider
        self._trading_day_checker = trading_day_checker
        # Market four-dimension providers are optional and lazily defaulted to
        # the akshare implementations (see ``_ensure_market_providers``) so the
        # production wiring stays a no-arg construct while tests inject fakes.
        self._market_breadth_provider = market_breadth_provider
        self._sector_provider = sector_provider
        self._dragon_tiger_provider = dragon_tiger_provider
        # 知识图谱 LLM 抽取（步骤 5b）：三者都是可选依赖——repo 或 adapter
        # 工厂缺席时该步骤以 ``daily_review_kg_extract_skipped`` 显式跳过
        # （不静默消失），复盘主链路不受影响。
        self._knowledge_graph_repository = knowledge_graph_repository
        self._model_adapter_factory = model_adapter_factory
        self._instrument_catalog_repository = instrument_catalog_repository

    def _ensure_market_providers(self) -> tuple[Any, Any, Any]:
        """Resolve (breadth, sector, dragon_tiger) providers, defaulting to akshare.

        Lazy so the akshare import (heavy) only happens on a real fire, and so a
        test that injects fakes never touches akshare. Injected providers win.
        """
        breadth = self._market_breadth_provider
        sector = self._sector_provider
        dragon = self._dragon_tiger_provider
        if breadth is None or sector is None or dragon is None:
            from doyoutrade.data.limit_pool_akshare import AkshareMarketBreadthProvider
            from doyoutrade.data.lhb_akshare import AkshareDragonTigerProvider
            from doyoutrade.data.sector_akshare import AkshareSectorProvider

            if breadth is None:
                breadth = AkshareMarketBreadthProvider()
            if sector is None:
                sector = AkshareSectorProvider()
            if dragon is None:
                dragon = AkshareDragonTigerProvider()
        return breadth, sector, dragon

    # --- knowledge-graph extraction (step 5b) -------------------------------

    async def _extract_kg_candidates(
        self,
        *,
        job_id: str,
        agent_id: str | None,
        asof: date,
        reply_text: str,
        journal_path: str,
        span: Any,
    ) -> dict[str, Any]:
        """从复盘 markdown 抽取 ``provenance='llm'`` 候选边（软失败）。

        返回结构化状态 dict（``status`` ∈ skipped/ok/error），并同步发
        debug event + ``daily_review.kg_*`` span attribute。任何异常都被
        捕获转结构化错误——本步骤永不阻断复盘投递。
        """
        if self._knowledge_graph_repository is None or self._model_adapter_factory is None:
            reason = (
                "knowledge_graph_repository_unwired"
                if self._knowledge_graph_repository is None
                else "model_adapter_factory_unwired"
            )
            span.set_attribute("daily_review.kg_extract_status", "skipped")
            await emit_debug_event(
                "daily_review_kg_extract_skipped",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "reason": reason,
                    "hint": "wire knowledge_graph_repository + model_adapter_factory "
                    "into DailyReviewExecutor (api/server.py) to enable KG extraction",
                },
            )
            return {"status": "skipped", "reason": reason}

        source_ref = (
            f"kb:{journal_path}" if journal_path else f"kb:journal/{asof.isoformat()}"
        )
        try:
            # 用复盘所用 agent 的模型路由做抽取（同一模型、同一账单口径）；
            # agent 未绑定路由时回退默认路由。
            route_name: str | None = None
            agent_repo = getattr(self._svc, "agent_repo", None)
            if agent_repo is not None and agent_id:
                agent = await agent_repo.get_agent(agent_id)
                route_name = (
                    str((agent or {}).get("model_route_name") or "").strip() or None
                )
            adapter = await self._model_adapter_factory(route_name)

            from doyoutrade.knowledge.graph_extraction import extract_and_apply

            result = await extract_and_apply(
                self._knowledge_graph_repository,
                adapter,
                reply_text,
                reference_date=asof.isoformat(),
                source_ref=source_ref,
                now=datetime.now(timezone.utc).replace(tzinfo=None),
                instrument_catalog_repository=self._instrument_catalog_repository,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
            logger.warning(
                "daily_review kg extraction failed job_id=%s asof=%s (%s): %s",
                job_id, asof, type(exc).__name__, exc,
            )
            span.set_attribute("daily_review.kg_extract_status", "error")
            await emit_debug_event(
                "daily_review_kg_extract_failed",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "source_ref": source_ref,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "hint": "KG extraction crashed; the review itself was "
                    "delivered — inspect the model route / kg tables",
                },
            )
            return {
                "status": "error",
                "error_code": "kg_extract_crashed",
                "message": f"{type(exc).__name__}: {exc}",
            }

        if result.get("status") != "ok":
            span.set_attribute("daily_review.kg_extract_status", "error")
            await emit_debug_event(
                "daily_review_kg_extract_failed",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "source_ref": source_ref,
                    "error_code": result.get("error_code"),
                    "message": result.get("message"),
                    "hint": "model call or output parse failed; see error_code",
                },
            )
            return result

        span.set_attribute("daily_review.kg_extract_status", "ok")
        span.set_attribute(
            "daily_review.kg_candidate_edge_count",
            int(result.get("candidate_count") or 0),
        )
        await emit_debug_event(
            "daily_review_kg_candidates_extracted",
            {
                "job_id": job_id,
                "asof": asof.isoformat(),
                "source_ref": source_ref,
                "skipped": result.get("skipped"),
                "candidate_count": result.get("candidate_count"),
                "edges_submitted": result.get("edges_submitted"),
                "apply": result.get("apply"),
                "warning_count": len(result.get("warnings") or []),
            },
        )
        return result

    # --- contract validation ----------------------------------------------

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(params, dict):
            return {
                "error_code": "invalid_task_params",
                "error": "params must be an object",
            }
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return {
                "error_code": "missing_agent_id",
                "error": "daily_review.params.agent_id is required",
                "field": "agent_id",
            }
        target = params.get("target_session_id")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            return {
                "error_code": "invalid_target_session_id",
                "error": "target_session_id must be a string or null",
                "field": "target_session_id",
            }
        account_id = params.get("account_id")
        if account_id is not None and (
            not isinstance(account_id, str) or not account_id.strip()
        ):
            return {
                "error_code": "invalid_account_id",
                "error": "account_id must be a string or null",
                "field": "account_id",
            }
        user_request = params.get("user_request")
        if user_request is not None and not isinstance(user_request, str):
            return {
                "error_code": "invalid_user_request",
                "error": "user_request must be a string or null",
                "field": "user_request",
            }
        return None

    # --- market four-dimension gather -------------------------------------

    async def _gather_market(
        self,
        *,
        job_id: str,
        asof: date,
        statement: dict[str, Any],
        span: Any,
    ) -> dict[str, Any] | None:
        """Soft-gather the whole-market 四维 (大盘/情绪/主线/个股) for the review.

        Three independent, best-effort fetches — a failure in any one emits a
        structured ``daily_review_market_unavailable`` debug event + a
        ``logger.warning`` and is skipped, NEVER failing the whole review
        (mirrors the KB-digest soft-gather; eastmoney endpoints rate-limit with
        ``RemoteDisconnected`` around after-close). Returns a ``market`` dict for
        the framing, or ``None`` when nothing was gathered (the template then
        says "market data unavailable" instead of fabricating行情).

        Dimensions:
          - ``breadth`` — the day's 涨停/跌停/炸板 + 连板梯队 + rule 情绪 label,
            via the market-breadth provider. On success it ALSO upserts the
            month's 情绪周期 log (``cycles/<YYYY-MM>/_sentiment.jsonl``), a
            non-fatal write (``sentiment_log_write_failed`` on error).
          - ``sector_heat_top`` — the top ``_SECTOR_HEAT_TOP_N`` concept boards
            by change (当日主线题材), via the sector provider.
          - ``holdings_lhb`` — 龙虎榜 hits among the user's held symbols (per
            statement positions), via the dragon-tiger provider; skipped when
            the book is empty.
        """
        breadth_provider, sector_provider, dragon_provider = (
            self._ensure_market_providers()
        )
        trade_date = asof.strftime("%Y%m%d")
        market: dict[str, Any] = {}

        # --- 1) 情绪 / 连板 / 涨停家数 (breadth) ---------------------------
        try:
            breadth = await breadth_provider.fetch_market_breadth(trade_date)
        except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
            logger.warning(
                "daily_review market breadth gather failed job_id=%s asof=%s (%s): %s",
                job_id, asof, type(exc).__name__, exc,
            )
            await emit_debug_event(
                "daily_review_market_unavailable",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "dimension": "breadth",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "hint": "market breadth (涨停/跌停/炸板) fetch failed "
                    "(eastmoney rate-limit or non-trading day); review composed "
                    "without today's 情绪/连板",
                },
            )
        else:
            sentiment = _breadth_to_sentiment_row(breadth)
            market["breadth"] = {
                "trade_date": breadth.trade_date,
                "limit_up_count": breadth.limit_up_count,
                "limit_down_count": breadth.limit_down_count,
                "broken_board_count": breadth.broken_board_count,
                "broken_board_rate": round(breadth.broken_board_rate, 4),
                "max_streak": breadth.max_streak,
                "ladder": dict(breadth.ladder),
                "sentiment": {
                    "label": sentiment["label"],
                    "reason": (
                        f"涨停 {breadth.limit_up_count} 家、"
                        f"跌停 {breadth.limit_down_count} 家、"
                        f"炸板 {breadth.broken_board_count} 家、"
                        f"最高 {breadth.max_streak} 连板、"
                        f"炸板率 {breadth.broken_board_rate:.0%}"
                    ),
                },
                "pool_errors": dict(breadth.pool_errors),
            }
            span.set_attribute("daily_review.market.sentiment_label", sentiment["label"])
            span.set_attribute("daily_review.market.limit_up_count", breadth.limit_up_count)
            # Persist the 情绪周期 row (idempotent per-day upsert). Non-fatal.
            await self._persist_sentiment_log(
                job_id=job_id, asof=asof, sentiment=sentiment, span=span
            )

        # --- 2) 主线题材 (concept-board heat) -----------------------------
        try:
            heat_rows = await sector_provider.get_sector_heat("concept")
        except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
            logger.warning(
                "daily_review market sector-heat gather failed job_id=%s asof=%s (%s): %s",
                job_id, asof, type(exc).__name__, exc,
            )
            await emit_debug_event(
                "daily_review_market_unavailable",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "dimension": "sector_heat",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "hint": "concept-board heat fetch failed (eastmoney "
                    "rate-limit); review composed without today's 主线题材",
                },
            )
        else:
            ranked = sorted(
                (r for r in heat_rows if r.change_pct is not None),
                key=lambda r: r.change_pct,
                reverse=True,
            )[:_SECTOR_HEAT_TOP_N]
            market["sector_heat_top"] = [
                {
                    "board_name": r.board_name,
                    "change_pct": r.change_pct,
                    "leader_stock": r.leader_stock,
                    "leader_change_pct": r.leader_change_pct,
                }
                for r in ranked
            ]
            span.set_attribute("daily_review.market.sector_heat_count", len(ranked))

        # --- 3) 持仓个股龙虎榜 (holdings dragon-tiger) --------------------
        holdings = _holdings_symbols(statement)
        if holdings:
            capped = holdings[:_MAX_HOLDINGS_LHB]
            try:
                lhb_rows = await dragon_provider.fetch_dragon_tiger(
                    trade_date, trade_date
                )
            except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                logger.warning(
                    "daily_review market holdings-lhb gather failed job_id=%s "
                    "asof=%s holdings=%d (%s): %s",
                    job_id, asof, len(capped), type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "daily_review_market_unavailable",
                    {
                        "job_id": job_id,
                        "asof": asof.isoformat(),
                        "dimension": "holdings_lhb",
                        "holdings_count": len(capped),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "龙虎榜 fetch failed (eastmoney rate-limit); "
                        "review composed without today's per-holding 龙虎榜",
                    },
                )
            else:
                held = set(capped)
                hits = [
                    {
                        "symbol": r.symbol,
                        "name": r.name,
                        "reason": r.reason,
                        "change_pct": r.change_pct,
                        "net_buy_amount": r.net_buy_amount,
                    }
                    for r in lhb_rows
                    if r.symbol in held
                ]
                market["holdings_lhb"] = hits
                span.set_attribute("daily_review.market.holdings_lhb_count", len(hits))

        if not market:
            span.set_attribute("daily_review.market.gathered", False)
            return None
        span.set_attribute("daily_review.market.gathered", True)
        return market

    async def _persist_sentiment_log(
        self, *, job_id: str, asof: date, sentiment: dict[str, Any], span: Any
    ) -> None:
        """Upsert the day's 情绪 row into ``cycles/<month>/_sentiment.jsonl``.

        Non-fatal: a write failure emits ``sentiment_log_write_failed`` + a
        ``logger.warning`` and returns — the review still proceeds (§错误可见性).
        """
        try:
            result = upsert_sentiment_log(asof, sentiment)
        except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
            logger.warning(
                "daily_review sentiment log upsert failed job_id=%s asof=%s (%s): %s",
                job_id, asof, type(exc).__name__, exc,
            )
            await emit_debug_event(
                "sentiment_log_write_failed",
                {
                    "job_id": job_id,
                    "asof": asof.isoformat(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "hint": "could not upsert cycles/<month>/_sentiment.jsonl; "
                    "today's 情绪 row is missing from the frontend timeline "
                    "(the review itself still ran)",
                },
            )
            return
        await emit_debug_event(
            "daily_review_sentiment_log_written",
            {
                "job_id": job_id,
                "asof": asof.isoformat(),
                "path": result.get("path"),
                "replaced": result.get("replaced"),
                "row_count": result.get("row_count"),
                "dropped": result.get("dropped"),
            },
        )
        span.set_attribute("daily_review.sentiment_log_path", str(result.get("path")))

    # --- runtime ----------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: JobRunContext) -> TaskResult:
        with tracer.start_as_current_span("cron.task.run") as span:
            span.set_attribute("cron.task.kind", self.kind)
            span.set_attribute("cron.job_id", ctx.job_id)
            span.set_attribute("cron.job_run_id", ctx.cron_job_run_id)

            agent_id = str(params.get("agent_id") or "").strip()
            target_session_id = params.get("target_session_id")
            if target_session_id is not None:
                target_session_id = str(target_session_id).strip() or None
            account_id = params.get("account_id")
            account_id = str(account_id).strip() if account_id else None
            user_request = str(
                params.get("user_request") or _DEFAULT_USER_REQUEST
            ).strip()

            asof = _asof_from_fired_at(ctx.fired_at)
            span.set_attribute("daily_review.asof", asof.isoformat())

            job = await self._cron_repo.get_job(ctx.job_id)
            if not job:
                span.set_attribute("cron.task.status", "failed")
                return TaskResult(status="failed", error=f"cron job not found: {ctx.job_id}")

            # 1) Trading-day gate (structured skip, never a silent empty run).
            if self._trading_day_checker is not None:
                try:
                    is_trading = await self._trading_day_checker(asof)
                except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                    logger.warning(
                        "daily_review trading-day check failed job_id=%s asof=%s (%s): %s",
                        ctx.job_id, asof, type(exc).__name__, exc,
                    )
                    await emit_debug_event(
                        "daily_review_calendar_check_failed",
                        {
                            "job_id": ctx.job_id,
                            "asof": asof.isoformat(),
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "hint": "trading-calendar lookup failed; proceeding "
                            "with the review (may run on a non-trading day)",
                        },
                    )
                else:
                    if not is_trading:
                        await emit_debug_event(
                            "daily_review_skipped",
                            {
                                "job_id": ctx.job_id,
                                "asof": asof.isoformat(),
                                "reason": "not_trading_day",
                                "hint": "fire landed on a non-trading day "
                                "(weekend/holiday); no review composed",
                            },
                        )
                        logger.info(
                            "daily_review skipped job_id=%s asof=%s reason=not_trading_day",
                            ctx.job_id, asof,
                        )
                        span.set_attribute("cron.task.status", "ok")
                        span.set_attribute("cron.delivery.status", "suppressed")
                        span.set_attribute("daily_review.skipped", True)
                        return TaskResult(
                            status="ok",
                            delivery_status="suppressed",
                            data={
                                "skipped": True,
                                "reason": "not_trading_day",
                                "asof": asof.isoformat(),
                            },
                        )

            # 2) Gather the live account statement.
            try:
                statement = await self._statement_provider(
                    account_id, asof, ctx.fired_at
                )
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                logger.exception(
                    "daily_review statement gather failed job_id=%s asof=%s",
                    ctx.job_id, asof,
                )
                await emit_debug_event(
                    "review_data_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "could not build the account statement (account "
                        "resolution / QMT connection); fix before the next fire",
                    },
                )
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute("cron.task.error", f"{type(exc).__name__}: {exc}")
                return TaskResult(
                    status="failed",
                    error=f"review_data_unavailable: {type(exc).__name__}: {exc}",
                )

            statement_errors = statement.get("errors") or []
            span.set_attribute("daily_review.trade_count", int(statement.get("trade_count") or 0))
            span.set_attribute("daily_review.statement_error_count", len(statement_errors))

            # 3) Gather the KB digest (soft: proceed with empty digest on failure).
            try:
                knowledge = build_daily_review_knowledge_digest(asof)
            except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                logger.warning(
                    "daily_review knowledge digest failed job_id=%s asof=%s (%s): %s",
                    ctx.job_id, asof, type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "daily_review_knowledge_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "KB digest build failed; review composed from "
                        "account data only",
                    },
                )
                knowledge = {"root_exists": False, "errors": [], "index_markdown": ""}

            # 3b) Compute the deterministic analytics layer (metrics + rule
            #     diagnostics). Soft: any failure here degrades the prompt to
            #     the legacy raw-statement-only shape — it never fails the
            #     whole review. The LLM is still expected to cite these when
            #     present.
            metrics: dict[str, Any] | None = None
            diagnostics: list[dict[str, str]] | None = None
            try:
                metrics = build_review_metrics(
                    statement,
                    knowledge=knowledge,
                    asof=asof.isoformat(),
                    lookback_days=1,
                )
                diagnostics = build_rule_diagnostics(
                    metrics, statement=statement, knowledge=knowledge
                )
                span.set_attribute(
                    "daily_review.metrics_error_count",
                    int(len(metrics.get("errors") or [])),
                )
                span.set_attribute(
                    "daily_review.diagnostics_count", int(len(diagnostics))
                )
                critical_count = sum(
                    1 for d in diagnostics if d.get("severity") == "critical"
                )
                warn_count = sum(
                    1 for d in diagnostics if d.get("severity") == "warn"
                )
                span.set_attribute("daily_review.diagnostics_critical", critical_count)
                span.set_attribute("daily_review.diagnostics_warn", warn_count)
            except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                logger.warning(
                    "daily_review analytics layer failed job_id=%s asof=%s (%s): %s",
                    ctx.job_id, asof, type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "daily_review_analytics_failed",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "metrics/diagnostics build failed; LLM composes "
                        "from raw statement only (no pre-computed indicators)",
                    },
                )

            # 3c) Soft-gather the whole-market 四维 (大盘/情绪/主线/个股) so the
            #     review reads as market-relative context, not only the user's
            #     own P&L. Any/all dimensions may be unavailable (eastmoney
            #     rate-limit); the helper returns None and the framing degrades
            #     gracefully. On breadth success it also upserts the月度 情绪周期
            #     log. This never fails the review.
            market: dict[str, Any] | None = None
            try:
                market = await self._gather_market(
                    job_id=ctx.job_id, asof=asof, statement=statement, span=span
                )
            except Exception as exc:  # noqa: BLE001 — defensive; gather is
                # already internally soft-guarded per dimension, but a bug in
                # the orchestration itself must not sink the whole review.
                logger.warning(
                    "daily_review market gather orchestration failed job_id=%s "
                    "asof=%s (%s): %s",
                    ctx.job_id, asof, type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "daily_review_market_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "dimension": "all",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "market gather orchestration raised; review "
                        "composed without today's market four-dimension block",
                    },
                )

            # 4) Compose via one agent turn.
            framing = render_daily_review_framing(
                job={"id": job["id"], "name": job["name"]},
                fired_at=ctx.fired_at.isoformat(),
                asof=asof.isoformat(),
                user_request=user_request,
                target_session_id=target_session_id,
                statement=statement,
                knowledge=knowledge,
                metrics=metrics,
                diagnostics=diagnostics,
                market=market,
            )

            agent_session_id: str | None = None
            reply_text = ""
            compose_failed_reason: str | None = None
            try:
                session = await self._svc.create_session(
                    agent_id=agent_id,
                    title=f"[每日复盘] {job['name']} {asof.isoformat()}",
                )
                agent_session_id = session["session_id"]
                span.set_attribute("cron.agent_session_id", agent_session_id)
                result = await self._svc.send_message(
                    session_id=agent_session_id,
                    content=framing,
                )
                messages = result.get("messages") if isinstance(result, dict) else None
                if isinstance(messages, list) and messages:
                    last = messages[-1]
                    if isinstance(last, dict):
                        reply_text = str(last.get("content") or "").strip()
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                compose_failed_reason = "compose_failed"
                logger.exception(
                    "daily_review compose failed job_id=%s asof=%s run_id=%s",
                    ctx.job_id, asof, ctx.cron_job_run_id,
                )
                await emit_debug_event(
                    "review_compose_failed",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "the review agent turn raised; check model route "
                        "/ agent config. A Python fallback journal will be "
                        "composed from the analytics layer and delivered.",
                    },
                )
                span.set_attribute("cron.task.compose_failed", True)
                span.set_attribute(
                    "cron.task.error", f"{type(exc).__name__}: {exc}"
                )

            if reply_text and not compose_failed_reason:
                # Happy path — proceed with the LLM-composed text.
                pass
            else:
                # Degraded path — build a Python-composed fallback journal so
                # the user is never left empty-handed. The original failure
                # (compose_failed / empty_reply) is still surfaced via debug
                # event above + the data['fallback_*'] flags below.
                if not compose_failed_reason:
                    compose_failed_reason = "empty_reply"
                    await emit_debug_event(
                        "review_compose_failed",
                        {
                            "job_id": ctx.job_id,
                            "asof": asof.isoformat(),
                            "reason": "empty_reply",
                            "hint": "the agent produced no review text; a "
                            "Python fallback journal will be composed from "
                            "the analytics layer",
                        },
                    )
                    span.set_attribute("cron.task.compose_failed", True)
                # If the analytics layer also failed earlier, we cannot compose
                # a meaningful fallback — that becomes a true failure (matches
                # the historical contract).
                if metrics is None:
                    span.set_attribute("cron.task.status", "failed")
                    return TaskResult(
                        status="failed",
                        agent_session_id=agent_session_id,
                        error=(
                            f"review_compose_failed: {compose_failed_reason}"
                            f" (analytics layer also unavailable, no fallback)"
                        ),
                    )
                fb_diagnostics = diagnostics or []
                reply_text = build_fallback_journal(
                    asof.isoformat(),
                    metrics,
                    fb_diagnostics,
                    reason=compose_failed_reason,
                )
                span.set_attribute("daily_review.fallback_applied", True)
                logger.info(
                    "daily_review fallback journal applied job_id=%s asof=%s "
                    "reason=%s diagnostics=%d",
                    ctx.job_id, asof, compose_failed_reason, len(fb_diagnostics),
                )
                await emit_debug_event(
                    "daily_review_fallback_applied",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "reason": compose_failed_reason,
                        "diagnostics_count": len(fb_diagnostics),
                        "hint": "LLM compose failed/empty; Python-composed "
                        "fallback journal was persisted and delivered",
                    },
                )

            # 5) Persist to the KB journal (non-fatal on failure — the composed
            #    review is still delivered so the user is not left empty-handed).
            journal_result: dict[str, Any] | None = None
            journal_error: str | None = None
            try:
                journal_result = write_daily_review_journal(
                    asof, reply_text, fired_at=ctx.fired_at
                )
            except Exception as exc:  # noqa: BLE001 — surfaced, non-fatal
                journal_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "daily_review journal write failed job_id=%s asof=%s (%s): %s",
                    ctx.job_id, asof, type(exc).__name__, exc,
                )
                await emit_debug_event(
                    "daily_review_journal_failed",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "could not write journal/<YYYY>/<asof>.md; the "
                        "review was still delivered but not persisted to the KB",
                    },
                )
            else:
                await emit_debug_event(
                    "daily_review_journal_appended"
                    if journal_result.get("appended")
                    else "daily_review_journal_written",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "path": journal_result.get("path"),
                        "bytes_written": journal_result.get("bytes_written"),
                        "appended": journal_result.get("appended"),
                        "title_synthesized": journal_result.get("title_synthesized"),
                        "index_refreshed": journal_result.get("index_refreshed"),
                    },
                )
                span.set_attribute(
                    "daily_review.journal_path", str(journal_result.get("path"))
                )

            # 5b) 知识图谱 LLM 抽取（软失败）：从刚写入的复盘 markdown 抽取
            #     provenance='llm' 候选边。fallback 复盘是确定性 Python 组稿
            #     （无新观点），跳过以省模型成本。任何失败只发结构化事件，
            #     不阻断投递。
            kg_extraction: dict[str, Any] | None = None
            if journal_result is not None and compose_failed_reason is None:
                kg_extraction = await self._extract_kg_candidates(
                    job_id=ctx.job_id,
                    agent_id=agent_id,
                    asof=asof,
                    reply_text=reply_text,
                    journal_path=str(journal_result.get("path") or ""),
                    span=span,
                )

            # 6) Deliver.
            delivery_status, delivery_info = await deliver_assistant_message_to_session(
                self._svc,
                target_session_id=target_session_id,
                content=reply_text,
                cron_job_id=ctx.job_id,
                cron_job_run_id=ctx.cron_job_run_id,
                cron_task_kind=self.kind,
            )
            span.set_attribute("cron.delivery.status", delivery_status)
            span.set_attribute("cron.task.status", "ok")
            delivery_error: str | None = None
            if delivery_status == "failed" and isinstance(delivery_info, dict):
                delivery_error = str(delivery_info.get("error") or "")

            return TaskResult(
                status="ok",
                agent_session_id=agent_session_id,
                delivery_status=delivery_status,
                delivery_error=delivery_error,
                data={
                    "asof": asof.isoformat(),
                    "trade_count": statement.get("trade_count"),
                    "statement_errors": statement_errors,
                    "knowledge_errors": knowledge.get("errors") or [],
                    "journal": journal_result,
                    "journal_error": journal_error,
                    "target_session_id": target_session_id,
                    "fallback_applied": compose_failed_reason is not None,
                    "fallback_reason": compose_failed_reason,
                    "metrics": metrics,
                    "diagnostics": diagnostics,
                    # The soft-gathered market four-dimension block (None when
                    # every dimension was unavailable / rate-limited).
                    "market": market,
                    # The LLM was asked to emit a trailing ```json block; if it
                    # did, surface the parsed structure here so downstream
                    # consumers (history listing, future review_reports UI) get
                    # the AI's own summary/diagnosis/recommendations in
                    # machine-readable form alongside the rule-derived
                    # ``diagnostics`` above. None for fallback runs.
                    "ai_structured": (
                        None
                        if compose_failed_reason
                        else parse_trailing_review_json(reply_text)
                    ),
                    # 步骤 5b 的知识图谱抽取结果（skipped/ok/error 结构化
                    # 状态；未装配或 fallback 复盘时为 skipped/None）。
                    "kg_extraction": kg_extraction,
                },
            )
