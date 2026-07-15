"""Post-cycle delivery for Task Triggers (Phase 2).

After a Trigger fires a cycle, if its ``delivery_json`` requests a push, render the
cycle digest into content and deliver it — to the originating session (reusing the
proven ``_deliver`` channel-forward primitive, which already persists the message AND
best-effort forwards it to the session's bound live channel) or straight to a durable
bound channel. The default render is a deterministic card (no LLM): cheaper,
reproducible, and it never touches the recursive-cron-guard surface.

``card`` mode renders the deterministic digest; ``prose`` mode instead runs ONE
compose-only agent turn (``composer_agent_id`` picks the agent; falls back to the
first active agent) that narrates the cycle — why a signal fired or why it didn't —
straight from ``cycle_runs.details`` (market_snapshot / signal_diagnostics). That
turn goes through the proven ``send_message`` path, so it records its own
``model_invocations`` + spans; the framing forbids tool use, keeping it off the
recursive-cron-guard surface. If composing yields nothing usable, delivery falls
back **visibly** to the deterministic card (ERROR log + span event) — a brief/full
trigger means "always notify", so a push is never silently dropped. On Feishu both
paths wrap into an interactive card via the existing builder. The post-cycle hook
lives OUTSIDE ``worker.run_cycle`` (the worker stays channel-agnostic) — it is
invoked by the TriggerScheduler and the manual ``/run`` endpoint after the cycle's
``cycle_runs`` row is persisted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from doyoutrade.assistant.cron_executors._deliver import deliver_assistant_message_to_session
from doyoutrade.assistant.main_agent import MAIN_AGENT_ID
from doyoutrade.assistant.prompt_templates import render_approval_framing, render_trigger_framing
from doyoutrade.assistant.signal_composer_agent import SIGNAL_COMPOSER_AGENT_ID
from doyoutrade.core.models import signal_context_from_intent_json
from doyoutrade.observability import get_logger, get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Pushes are read by A-share operators, so cycle timestamps surface in Asia/Shanghai
# (UTC+8) — matching the runtime-context reminder in assistant/service.py. We store
# naive UTC in cycle_runs and convert only at the display edge.
_BEIJING_TZ = timezone(timedelta(hours=8))


def _details(digest: dict | None) -> dict:
    return (digest or {}).get("details") or {}


def _format_processing_time(digest: dict | None) -> str:
    """`wall_started_at` (naive UTC in the digest) → 北京时间 ``YYYY-MM-DD HH:MM:SS``.

    This is the real wall-clock moment the cycle began processing the strategy —
    always populated (unlike ``cycle_time_utc``, which only exists in simulated
    runs). Returns ``""`` when the field is absent or unparseable: delivery is
    best-effort and a display field must never break the push. A parse failure is
    still logged with the offending type/value so a schema drift in
    ``wall_started_at`` stays visible rather than silently dropping the timestamp.
    """
    raw = (digest or {}).get("wall_started_at")
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        logger.warning(
            "trigger delivery: unparseable wall_started_at type=%s value=%r err=%s",
            type(raw).__name__, raw, exc,
        )
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def has_actionable(digest: dict | None) -> bool:
    """True when the cycle produced something worth pushing (intents/fills/submits)."""
    d = _details(digest)
    return bool(d.get("position_intents")) or bool(d.get("fills")) or bool(
        (digest or {}).get("submitted_count")
    )


def _fmt_pct(value: Any) -> str:
    """`（+2.16%）` / `（-1.30%）` / `` (empty when pct unknown)."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    sign = "+" if f > 0 else ""
    return f"（{sign}{f:.2f}%）"


def _symbol_label(symbol: str, symbol_names: dict[str, str] | None) -> str:
    """`工商银行（601398.SH）` when a display name is known, else the bare code.

    Keeps the operator-facing 行情/判断/动作 rows consistent with the 持仓 block
    (which already names holdings): a bare symbol code is opaque to an A-share
    operator scanning a push card.
    """
    name = (symbol_names or {}).get(symbol) or ""
    name = name.strip()
    return f"{name}（{symbol}）" if name else str(symbol)


def _market_lines(market: dict, symbol_names: dict[str, str] | None = None) -> list[str]:
    """Per-symbol `最新价（涨跌幅）` rows from ``details.market_snapshot``."""
    out: list[str] = []
    for sym in sorted(market):
        info = market.get(sym) or {}
        if not isinstance(info, dict):
            continue
        lp = info.get("last_price")
        out.append(
            f"- {_symbol_label(sym, symbol_names)} {lp if lp is not None else '—'}"
            f"{_fmt_pct(info.get('pct_change'))}".rstrip()
        )
    return out


def _diagnostic_lines(diags: dict, symbol_names: dict[str, str] | None = None) -> list[str]:
    """Per-symbol `方向 [标签] 理由` rows from ``details.signal_diagnostics``.

    This is the "为什么本轮没动手" narration — direction / decision tag /
    rationale straight from each symbol's Signal (see signal_sdk Signal.to_dict).
    """
    out: list[str] = []
    for sym in sorted(diags):
        sig = diags.get(sym) or {}
        if not isinstance(sig, dict):
            continue
        direction = sig.get("direction") or ""
        tag = sig.get("tag") or ""
        rationale = sig.get("rationale") or ""
        tag_part = f"[{tag}] " if tag else ""
        out.append(
            f"- {_symbol_label(sym, symbol_names)} {direction} {tag_part}{rationale}".rstrip()
        )
    return out


def render_trigger_digest(
    trigger: Any,
    digest: dict | None,
    *,
    no_signal_mode: str = "brief",
    task_name: str = "",
    symbol_names: dict[str, str] | None = None,
) -> str:
    """Deterministic markdown summary of a cycle digest for a trigger push.

    Covers the states a §错误可见性-correct card must not drop: error digest,
    actionable signals/fills, and the explicit no-signal case. ``no_signal_mode``
    controls verbosity: ``brief`` (and ``silent``, which only reaches here when a
    signal exists) renders just intents/fills or the one-line no-signal notice;
    ``full`` additionally appends a 行情 section (last price + 涨跌幅 from
    ``details.market_snapshot``) and a 判断 section (direction / tag / rationale
    from ``details.signal_diagnostics``) so an empty cycle still explains itself.

    ``task_name`` / ``symbol_names`` enrich the push with the task's display name
    and each stock's Chinese name (工商银行) resolved best-effort by the caller
    from the task / instrument catalogs — an operator scanning a push must not
    see only opaque ids / codes. Missing values degrade gracefully to the bare
    trigger name / symbol code (never raise at the display edge).
    """
    name = getattr(trigger, "name", "") or getattr(trigger, "id", "trigger")
    digest = digest or {}
    processed_at = _format_processing_time(digest)
    when = f"（{processed_at}）" if processed_at else ""
    title = f"【{name} · 策略信号】"
    if task_name:
        title += f"\n任务：{task_name}"
    if digest.get("cycle_failed"):
        return (
            f"⚠️ **{name}**{when} 本轮运行失败："
            f"{digest.get('failure_message') or digest.get('status')}"
        )
    d = _details(digest)
    intents = d.get("position_intents") or []
    fills = d.get("fills") or []
    lines = [title]
    if processed_at:
        lines.append(f"处理时间：{processed_at}（北京时间）")
    if intents:
        lines.append("意图：")
        for it in intents[:20]:
            if isinstance(it, dict):
                sym = it.get("symbol", "")
                act = it.get("action") or it.get("side") or ""
                amt = it.get("amount")
                rat = it.get("rationale") or it.get("signal_tag") or ""
                line = f"- {_symbol_label(sym, symbol_names)} {act} {amt if amt is not None else ''} {rat}".rstrip()
                # A live order held for human approval is NOT placed — mark it so
                # the digest never reads as "already executed" (the approval card
                # is the actionable surface; this is just the signal context).
                if it.get("pending_approval"):
                    line += " （待审批）"
                lines.append(line)
    if fills:
        lines.append("成交：")
        for f in fills[:20]:
            if isinstance(f, dict):
                lines.append(
                    f"- {_symbol_label(f.get('symbol', ''), symbol_names)} {f.get('side', '')} "
                    f"{f.get('quantity', '')}@{f.get('price', '')}"
                )
    if not intents and not fills:
        lines.append("本轮无可执行信号。")

    if no_signal_mode == "full":
        market_lines = _market_lines(d.get("market_snapshot") or {}, symbol_names)
        if market_lines:
            lines.append("行情：")
            lines.extend(market_lines)
        diag_lines = _diagnostic_lines(d.get("signal_diagnostics") or {}, symbol_names)
        if diag_lines:
            lines.append("判断：")
            lines.extend(diag_lines)
    return "\n".join(lines)


def _extract_reply_text(result: Any) -> str:
    """Final assistant text from a ``send_message`` result (see service.send_message)."""
    messages = result.get("messages") if isinstance(result, dict) else None
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            return str(last.get("content") or "").strip()
    return ""


async def _resolve_default_agent_id(assistant_service: Any) -> str | None:
    """Default composer agent when a prose trigger omits ``composer_agent_id``.

    Prefers the dedicated signal-card composer agent (compose-only: no tools,
    no skills, lean prompt → minimal noise + deterministic card shape), then
    falls back to the code-fixed main agent, then the first active agent (the
    repo orders is_builtin/is_default first). Routing prose pushes through the
    composer agent keeps the compose turn OFF the main agent's full CLI/cron/
    skill surface — see doyoutrade/assistant/signal_composer_agent.py."""
    repo = getattr(assistant_service, "agent_repo", None)
    if repo is None:
        return None
    # 1) The dedicated composer agent — preferred for prose card composition.
    try:
        composer = await repo.get_agent(SIGNAL_COMPOSER_AGENT_ID)
    except Exception:
        composer = None
        logger.exception("trigger prose: fetching signal composer agent failed")
    if isinstance(composer, dict) and composer.get("id") and composer.get("status") == "active":
        return str(composer["id"])
    # 2) The code-fixed main agent (well-known id) — robust fallback if the
    # composer row is missing (e.g. migration not yet applied on a legacy boot).
    try:
        main = await repo.get_agent(MAIN_AGENT_ID)
    except Exception:
        main = None
        logger.exception("trigger prose: fetching main agent failed")
    if isinstance(main, dict) and main.get("id") and main.get("status") == "active":
        return str(main["id"])
    # 3) Last resort: first active agent (repo serializes builtins first → the
    # first active row is a usable default).
    try:
        agents = await repo.list_agents(include_inactive=False)
    except Exception:
        logger.exception("trigger prose: listing active agents failed")
        return None
    for agent in agents or []:
        if isinstance(agent, dict):
            aid = agent.get("id") or agent.get("agent_id")
            if aid:
                return str(aid)
    return None


# Fixed card skeleton the prose compose turn fills. The title line and the four
# section headers are reproduced VERBATIM by the backend renderer — the composer
# agent only authors each section's BODY (returned as a JSON object). This makes
# the card title + section shape deterministic across fires (the model cannot
# rename headers or invent a layout), while the section CONTENT is still
# Agent-narrated. See trigger_digest_framing.j2 for the JSON contract.
_COMPOSER_SECTION_KEYS = ("market", "judgement", "account", "action")


@dataclass
class ComposeResult:
    """Outcome of a prose compose turn.

    ``text`` is the delivered card body (the fixed-shape skeleton, or the
    freetext card on Tier 2). ``sections`` carries the Agent-authored section
    bodies when the composer returned structured JSON (Tier 1) — used to render
    a rich Feishu card with an「AI 解读」panel; ``None`` otherwise.
    """

    text: str
    sections: dict[str, str] | None = None


def _extract_composer_json(text: str) -> dict[str, str] | None:
    """Leniently extract the composer's section JSON from an LLM reply.

    The framing asks for a bare JSON object, but models sometimes wrap it in a
    ```json fence or surround it with prose. This finds the outermost ``{ ... }``
    block and parses it. Returns ``None`` on any parse failure so the caller can
    fall back visibly to the deterministic card (CLAUDE.md §错误可见性 — never
    silently ship a malformed push). Only string values are kept; non-strings
    are coerced to str so the renderer always sees text.
    """
    if not text:
        return None
    raw = text.strip()
    # Strip a surrounding ```json ... ``` / ``` ... ``` fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    # If there's still non-JSON text around it, carve out the outermost braces.
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        raw = raw[start : end + 1]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    sections: dict[str, str] = {}
    for key in _COMPOSER_SECTION_KEYS:
        value = parsed.get(key)
        if value is None:
            continue
        sections[key] = str(value).strip()
    # Require at least one section; otherwise treat as unusable.
    return sections or None


def _render_composer_skeleton(
    *, trigger_name: str, processed_at: str, sections: dict[str, str]
) -> str:
    """Render the fixed card skeleton from the composer's section JSON.

    Title line + the four section headers are LITERAL here (backend-owned), so
    they never vary across fires. Missing sections fall back to "数据缺失"
    rather than silently dropping a section — the operator must see that the
    composer skipped it.
    """
    title = f"【{trigger_name} · 策略信号】"
    head = [title]
    if processed_at:
        head.append(f"处理时间：{processed_at}（北京时间）")
    head.append("")
    head.append("行情：")
    head.append(sections.get("market") or "行情数据缺失")
    head.append("")
    head.append("判断：")
    head.append(sections.get("judgement") or "判断数据缺失")
    head.append("")
    head.append("账户：")
    head.append(sections.get("account") or "账户数据缺失")
    head.append("")
    head.append("本轮动作：")
    head.append(sections.get("action") or "本轮无可执行信号，策略维持观望。")
    return "\n".join(head)


async def _compose_via_agent(
    assistant_service: Any,
    *,
    trigger: Any,
    digest: dict | None,
    no_signal_mode: str,
    run_id: str | None,
    composer_agent_id: str | None,
) -> ComposeResult | None:
    """Run ONE compose-only agent turn that narrates the cycle digest.

    Returns a :class:`ComposeResult` (``text`` = delivered card body, ``sections``
    = the composer's structured section bodies when available), or ``None`` when
    nothing usable was produced (no resolvable agent / LLM error / empty /
    ``[SILENT]``) so the caller can fall back visibly to the deterministic card.
    Never raises into the fire.
    """
    trg_id = getattr(trigger, "id", "") or ""
    with tracer.start_as_current_span("trigger.delivery.compose") as span:
        span.set_attribute("trigger.id", trg_id)
        if run_id:
            span.set_attribute("run_id", run_id)
        agent_id = (composer_agent_id or "").strip() or await _resolve_default_agent_id(
            assistant_service
        )
        if not agent_id:
            span.set_attribute("compose.status", "no_agent")
            span.add_event("trigger_compose_no_agent")
            logger.error(
                "trigger prose: no composer agent resolvable (composer_agent_id unset and "
                "no active agent) trigger_id=%s run_id=%s",
                trg_id, run_id,
            )
            return None
        span.set_attribute("compose.agent_id", agent_id)

        name = getattr(trigger, "name", "") or trg_id or "trigger"
        fired = getattr(trigger, "last_fired_at", None)
        fired_at = fired.isoformat() if hasattr(fired, "isoformat") else (str(fired) if fired else "")
        framing = render_trigger_framing(
            trigger_name=name,
            trigger_id=trg_id,
            fired_at=fired_at,
            processed_at=_format_processing_time(digest),
            run_mode=str((digest or {}).get("run_mode") or ""),
            digest=digest or {},
            no_signal_mode=no_signal_mode,
        )
        try:
            session = await assistant_service.create_session(
                agent_id=agent_id, title=f"[Trigger] {name}"
            )
            session_id = session["session_id"]
            span.set_attribute("compose.session_id", session_id)
            # Attribute the composer's model invocation to the CYCLE's run_id +
            # task_id so it appears under 周期详情 model invocations (run_id 贯穿),
            # instead of an opaque asst-run id the operator can't drill back from.
            result = await assistant_service.send_message(
                session_id=session_id,
                content=framing,
                source_attribution={
                    "run_id": run_id,
                    "task_id": getattr(trigger, "task_id", None),
                },
            )
        except Exception as exc:  # noqa: BLE001 — surfaced via span + ERROR log, then fallback
            span.set_attribute("compose.status", "failed")
            span.add_event(
                "trigger_compose_failed", {"error": f"{type(exc).__name__}: {exc}"}
            )
            logger.exception(
                "trigger prose compose LLM call failed trigger_id=%s run_id=%s agent_id=%s",
                trg_id, run_id, agent_id,
            )
            return None

        text = _extract_reply_text(result)
        if not text or text == "[SILENT]":
            span.set_attribute("compose.status", "empty")
            span.add_event("trigger_compose_empty")
            logger.warning(
                "trigger prose compose produced empty/[SILENT] text trigger_id=%s run_id=%s "
                "agent_id=%s; will fall back to deterministic card",
                trg_id, run_id, agent_id,
            )
            return None
        # Tier 1 — structured: the composer returned its section contents as a
        # JSON object. The backend renders the fixed title + section headers
        # from it (deterministic card shape; section BODIES are LLM-narrated).
        sections = _extract_composer_json(text)
        if sections is not None:
            span.set_attribute("compose.status", "ok")
            span.set_attribute("compose.sections", ",".join(sorted(sections.keys())))
            return ComposeResult(
                text=_render_composer_skeleton(
                    trigger_name=name,
                    processed_at=_format_processing_time(digest),
                    sections=sections,
                ),
                sections=sections,
            )
        # Tier 2 — the model ignored the JSON ask but DID reproduce the fixed
        # title verbatim (observed on chat-oriented models that resist JSON). Ship
        # its narrated card as-is: the title is deterministic, the section COVERAGE
        # is guided by the framing (行情/判断/账户/本轮动作). This keeps the Agent
        # interpretation on models that can't do structured output.
        expected_title = f"【{name} · 策略信号】"
        if expected_title in text:
            span.set_attribute("compose.status", "ok_freetext")
            span.add_event("trigger_compose_freetext_with_title")
            return ComposeResult(text=text.strip(), sections=None)
        # Tier 3 — neither JSON nor the fixed title: free-form text the operator
        # can't tell apart from a normal push. Fall back visibly to the
        # deterministic card (same fixed title) instead of shipping noise.
        span.set_attribute("compose.status", "unparseable")
        span.add_event("trigger_compose_unparseable", {"reply_head": text[:200]})
        logger.warning(
            "trigger prose compose reply had no usable JSON and no fixed title "
            "trigger_id=%s run_id=%s agent_id=%s reply_head=%r; "
            "falling back to deterministic card",
            trg_id, run_id, agent_id, text[:200],
        )
        return None


async def _compose_approval_via_agent(
    assistant_service: Any,
    *,
    payload: dict,
    composer_agent_id: str | None,
    run_id: str | None,
) -> str | None:
    """Run ONE compose-only agent turn that narrates a pending LIVE order approval.

    Mirrors :func:`_compose_via_agent` (the signal-digest composer) so the
    approval push gets the SAME agent-authored richness — names the stock, sums
    up the signal, market and reason — instead of only the deterministic
    template. Returns the composed narration (card body), or ``None`` on no
    resolvable agent / LLM error / empty so the caller falls back visibly to the
    deterministic rich card (功能不阉割). Never raises into the fire.
    """
    approval_id = str(payload.get("approval_id") or "")
    with tracer.start_as_current_span("approval.delivery.compose") as span:
        span.set_attribute("approval.id", approval_id)
        if run_id:
            span.set_attribute("run_id", run_id)
        agent_id = (composer_agent_id or "").strip() or await _resolve_default_agent_id(
            assistant_service
        )
        if not agent_id:
            span.set_attribute("compose.status", "no_agent")
            span.add_event("approval_compose_no_agent")
            logger.error(
                "approval prose: no composer agent resolvable approval_id=%s run_id=%s",
                approval_id, run_id,
            )
            return None
        span.set_attribute("compose.agent_id", agent_id)
        framing = render_approval_framing(order=payload)
        try:
            session = await assistant_service.create_session(
                agent_id=agent_id, title=f"[Approval] {payload.get('symbol') or approval_id}"
            )
            session_id = session["session_id"]
            span.set_attribute("compose.session_id", session_id)
            # Attribute the approval narration's model invocation to the cycle
            # run_id + task_id (run_id 贯穿) so it's traceable from 周期详情.
            result = await assistant_service.send_message(
                session_id=session_id,
                content=framing,
                source_attribution={
                    "run_id": run_id,
                    "task_id": payload.get("task_id"),
                },
            )
        except Exception as exc:  # noqa: BLE001 — surfaced via span + ERROR log, then fallback
            span.set_attribute("compose.status", "failed")
            span.add_event("approval_compose_failed", {"error": f"{type(exc).__name__}: {exc}"})
            logger.exception(
                "approval prose compose LLM call failed approval_id=%s run_id=%s agent_id=%s",
                approval_id, run_id, agent_id,
            )
            return None
        text = _extract_reply_text(result)
        if not text or text == "[SILENT]":
            span.set_attribute("compose.status", "empty")
            span.add_event("approval_compose_empty")
            logger.warning(
                "approval prose compose produced empty/[SILENT] text approval_id=%s run_id=%s "
                "agent_id=%s; falling back to deterministic card",
                approval_id, run_id, agent_id,
            )
            return None
        span.set_attribute("compose.status", "ok")
        return text


async def fetch_approval_signal_snapshot(
    cycle_run_repository: Any, run_id: str | None, symbol: str | None
) -> dict[str, str]:
    """Signal-time 行情 + 判断 for an order's symbol, from its cycle digest.

    Returns ``{"last_price", "pct_change", "direction"}`` (display strings, empty
    when unavailable) so the approval card carries the same 行情/判断 the pure
    signal digest does — captured at the SIGNAL's cycle time (``market_snapshot``
    / ``signal_diagnostics``), NOT a later live price (which would be wrong to
    approve against). Best-effort: missing run/repo/digest or a lookup error
    yields empties + a WARNING — the card still renders its intent-derived facts.
    """
    empty = {"last_price": "", "pct_change": "", "direction": ""}
    if cycle_run_repository is None or not run_id or not symbol:
        return dict(empty)
    try:
        digest = await cycle_run_repository.get_by_run_id(run_id)
    except Exception:
        logger.warning(
            "approval card: cycle digest lookup failed run_id=%s", run_id, exc_info=True
        )
        return dict(empty)
    if not isinstance(digest, dict):
        return dict(empty)
    details = _details(digest)
    market = (details.get("market_snapshot") or {}).get(symbol) or {}
    diag = (details.get("signal_diagnostics") or {}).get(symbol) or {}
    last_price = market.get("last_price") if isinstance(market, dict) else None
    pct = market.get("pct_change") if isinstance(market, dict) else None
    direction = diag.get("direction") if isinstance(diag, dict) else None

    def _pct_plain(value: Any) -> str:
        if value is None:
            return ""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return ""
        return f"{'+' if f > 0 else ''}{f:.2f}%"

    return {
        "last_price": "" if last_price is None else str(last_price),
        "pct_change": _pct_plain(pct),
        "direction": str(direction or ""),
    }


async def fetch_symbol_name(instrument_catalog_repository: Any, symbol: str | None) -> str:
    """Resolve an instrument's display name (工商银行) for a symbol (601398.SH).

    Best-effort: no repo / no symbol / unknown symbol / lookup error → "" (the
    card falls back to the bare symbol). Lets every approval surface name the
    stock instead of showing only the opaque code.
    """
    if instrument_catalog_repository is None or not symbol:
        return ""
    try:
        row = await instrument_catalog_repository.get(symbol)
    except Exception:
        logger.warning("approval card: symbol name lookup failed symbol=%s", symbol, exc_info=True)
        return ""
    if isinstance(row, dict):
        return str(row.get("display_name") or "")
    return ""


def _collect_digest_symbols(digest: dict | None) -> list[str]:
    """Every symbol referenced by a cycle digest (行情 / 判断 / 意图 / 成交).

    De-duplicated, order-preserving — the universe per cycle is small, so the
    caller can resolve each name without a batch endpoint.
    """
    d = _details(digest)
    seen: list[str] = []
    pools = [
        list((d.get("market_snapshot") or {}).keys()),
        list((d.get("signal_diagnostics") or {}).keys()),
    ]
    for it in d.get("position_intents") or []:
        if isinstance(it, dict):
            pools.append([it.get("symbol")])
    for f in d.get("fills") or []:
        if isinstance(f, dict):
            pools.append([f.get("symbol")])
    for pool in pools:
        for sym in pool:
            sym = str(sym or "").strip()
            if sym and sym not in seen:
                seen.append(sym)
    return seen


async def _resolve_symbol_names(
    instrument_catalog_repository: Any, symbols: Iterable[str]
) -> dict[str, str]:
    """{symbol: display_name} for the resolvable symbols. Best-effort, never raises.

    Unknown / missing / errored lookups are simply omitted so the renderer falls
    back to the bare code (a display field must never break the push).
    """
    out: dict[str, str] = {}
    if instrument_catalog_repository is None:
        return out
    for sym in symbols:
        name = await fetch_symbol_name(instrument_catalog_repository, sym)
        if name:
            out[str(sym)] = name
    return out


async def _resolve_task_name(task_repository: Any, task_id: str | None) -> str:
    """A task's display name for the push card. Best-effort: "" when unknown.

    The footer already carried the opaque ``task_id``; the operator-facing push
    must also show the human name (e.g. ``银行网格``) so the card is scannable
    without opening the console.
    """
    if task_repository is None or not task_id:
        return ""
    try:
        snapshot = await task_repository.get_task(task_id)
    except Exception:
        logger.info("trigger delivery: task name lookup failed task_id=%s", task_id, exc_info=True)
        return ""
    return str(getattr(snapshot, "name", "") or "").strip()


async def deliver_pending_approval_cards(
    assistant_service: Any,
    *,
    trigger: Any,
    run_id: str | None,
    approval_gate: Any,
    cycle_run_repository: Any = None,
    instrument_catalog_repository: Any = None,
) -> int:
    """Send a Feishu approval card for each pending approval this fire created.

    A live-trading order held for human approval (``QueuedApprovalGate`` returned
    pending) needs a notification an operator can act on. This pushes one
    interactive card per pending approval for ``run_id`` to the trigger's Feishu
    channel target (``delivery_json.target`` kind=channel) — the card's
    approve/reject buttons resolve the execution-side gate (see FeishuChannel
    ``trade_approval_resolve``). The web Approvals page works regardless; this is
    the push. Best-effort: never raises into the fire, returns the count sent.

    Independent of ``delivery.mode``: a pending order must be notified even when
    digest pushes are off — but it still needs a resolvable Feishu channel target
    to address (no target → web-only, logged at INFO).
    """
    if assistant_service is None or approval_gate is None or not run_id:
        return 0
    if not hasattr(approval_gate, "list_pending"):
        return 0
    delivery = getattr(trigger, "delivery_json", None)
    target = delivery.get("target") if isinstance(delivery, dict) else None
    if not isinstance(target, dict) or target.get("kind") != "channel":
        return 0
    # Agent-composed (prose) approval push when the trigger is configured prose —
    # same switch as the signal digest, so the approval card gets the same
    # Agent-authored richness; otherwise the deterministic rich card.
    prose_mode = (delivery.get("mode") if isinstance(delivery, dict) else None) == "prose"
    composer_agent_id = delivery.get("composer_agent_id") if isinstance(delivery, dict) else None
    channel_id = target.get("channel_id")
    chat_id = target.get("chat_id")
    if not channel_id or not chat_id:
        return 0
    manager = getattr(assistant_service, "channel_manager", None)
    channel = manager.get(channel_id) if manager is not None else None
    if channel is None or getattr(channel, "channel_type", "") != "feishu":
        return 0
    send_card = getattr(channel, "send_trade_approval_card", None)
    if not callable(send_card):
        return 0
    try:
        pending = await approval_gate.list_pending()
    except Exception:
        logger.exception("approval card delivery: list_pending failed run_id=%s", run_id)
        return 0
    sent = 0
    for ap in pending or []:
        if getattr(ap, "run_id", None) != run_id:
            continue
        created_at = getattr(ap, "created_at", None)
        expires_at = getattr(ap, "expires_at", None)
        # Signal + order context (理由 / signal_tag / 真实策略名 / 限价 / 订单类型 /
        # 有效期) from the persisted intent so the Feishu card shows the same 信号
        # section as the web/Chat card. (``ap.mode`` is the run mode e.g. "live" —
        # NOT the strategy; the real strategy_tag lives in the intent payload.)
        signal = signal_context_from_intent_json(getattr(ap, "intent_payload", None))
        # Signal-time 行情/判断 for the order's symbol (parity with the pure
        # signal digest), keyed by the approval's originating run_id.
        snapshot = await fetch_approval_signal_snapshot(
            cycle_run_repository, run_id, getattr(ap, "symbol", None)
        )
        symbol_name = await fetch_symbol_name(
            instrument_catalog_repository, getattr(ap, "symbol", None)
        )
        payload = {
            "approval_id": ap.approval_id,
            "intent_id": ap.intent_id,
            "task_id": getattr(ap, "task_id", None),
            "run_id": run_id,
            "symbol": getattr(ap, "symbol", None),
            "symbol_name": symbol_name,
            "action": getattr(ap, "action", None),
            "notional": getattr(ap, "notional", None),
            "strategy_tag": signal["strategy_tag"] or getattr(ap, "mode", None),
            "rationale": signal["rationale"],
            "signal_tag": signal["signal_tag"],
            "price_reference": signal["price_reference"],
            "order_type": signal["order_type"],
            "tif": signal["tif"],
            "exit_reason": signal["exit_reason"],
            "last_price": snapshot["last_price"],
            "pct_change": snapshot["pct_change"],
            "direction": snapshot["direction"],
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
            "expires_at": expires_at.isoformat() if hasattr(expires_at, "isoformat") else None,
        }
        # Prose mode: let the agent narrate (names the stock, sums signal/market/
        # reason). On any compose failure → narration None → deterministic rich
        # card (the buttons + facts are identical either way; 功能不阉割).
        narration = None
        if prose_mode:
            narration = await _compose_approval_via_agent(
                assistant_service,
                payload=payload,
                composer_agent_id=composer_agent_id,
                run_id=run_id,
            )
        try:
            await send_card(chat_id, payload, narration)
            sent += 1
            logger.info(
                "approval card delivered approval_id=%s chat_id=%s trigger_id=%s run_id=%s prose=%s",
                ap.approval_id, chat_id, getattr(trigger, "id", None), run_id, bool(narration),
            )
        except Exception:
            logger.exception(
                "approval card delivery failed approval_id=%s chat_id=%s",
                ap.approval_id, chat_id,
            )
    return sent


def _format_iso_beijing(raw: Any) -> str:
    """ISO/naive-UTC instant → 北京时间 ``YYYY-MM-DD HH:MM:SS`` (empty on failure)."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        logger.warning("approval result card: unparseable fill timestamp value=%r", raw)
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _decimal_str(value: Any) -> str:
    """Render a numeric value as a plain decimal string (金额十进制 — no float repr).

    Returns "" when the value is missing/unparseable so the card shows a sentinel
    rather than a misleading 0.
    """
    if value is None or value == "":
        return ""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format(d.normalize(), "f")


async def _resolve_channel_target(trigger_repository: Any, task_id: str | None) -> dict | None:
    """First ``kind=channel`` Feishu delivery target among a task's triggers.

    The approval card was addressed via the firing trigger's ``delivery_json``; the
    resume sweep no longer has that trigger in hand, so re-resolve it from the task.
    Returns ``{"channel_id", "chat_id"}`` or None (no channel-bound trigger → the
    order outcome stays web-only, logged by the caller).
    """
    if trigger_repository is None or not task_id:
        return None
    try:
        triggers = await trigger_repository.list_for_task(task_id)
    except Exception:
        logger.warning(
            "approval result card: list_for_task failed task_id=%s", task_id, exc_info=True
        )
        return None
    for trg in triggers or []:
        delivery = getattr(trg, "delivery_json", None)
        target = delivery.get("target") if isinstance(delivery, dict) else None
        if not isinstance(target, dict) or target.get("kind") != "channel":
            continue
        channel_id = target.get("channel_id")
        chat_id = target.get("chat_id")
        if channel_id and chat_id:
            return {"channel_id": channel_id, "chat_id": chat_id}
    return None


async def deliver_approval_result_card(
    assistant_service: Any,
    *,
    approval: Any,
    outcome: str,
    fill: dict | None = None,
    error: str = "",
    trigger_repository: Any = None,
    cycle_run_repository: Any = None,
    instrument_catalog_repository: Any = None,
) -> str:
    """Push a deterministic order-RESULT card after an approved order is dispatched.

    The approve→fill is async (the scheduler resume sweep), so 已批准 alone never
    tells the operator whether the order ACTUALLY filled. This pushes one receipt
    card to the task's Feishu channel: ``filled`` (green, actual 成交数量/价/额/时间
    from the persisted fill) or ``failed``/``abandoned`` (red, planned 金额 + 失败原因).
    No Agent, no buttons — pure deterministic facts. Best-effort: returns a status
    string (``delivered`` / ``no_channel_target`` / ``channel_unavailable`` /
    ``send_failed`` / ``disabled``) for §错误可见性, never raises into the sweep.
    """
    if assistant_service is None or approval is None:
        return "disabled"
    task_id = getattr(approval, "task_id", None)
    target = await _resolve_channel_target(trigger_repository, task_id)
    if target is None:
        logger.info(
            "approval result card: no channel target task_id=%s approval_id=%s outcome=%s "
            "(order outcome stays web-only)",
            task_id, getattr(approval, "approval_id", None), outcome,
        )
        return "no_channel_target"
    channel_id = target["channel_id"]
    chat_id = target["chat_id"]
    manager = getattr(assistant_service, "channel_manager", None)
    channel = manager.get(channel_id) if manager is not None else None
    send = getattr(channel, "send_trade_approval_result_card", None)
    if (
        channel is None
        or getattr(channel, "channel_type", "") != "feishu"
        or not callable(send)
    ):
        logger.warning(
            "approval result card: channel unavailable channel_id=%s approval_id=%s",
            channel_id, getattr(approval, "approval_id", None),
        )
        return "channel_unavailable"

    symbol = getattr(approval, "symbol", None)
    symbol_name = await fetch_symbol_name(instrument_catalog_repository, symbol)
    signal = signal_context_from_intent_json(getattr(approval, "intent_payload", None))
    run_id = getattr(approval, "run_id", None) or ""
    fill = fill if isinstance(fill, dict) else {}
    qty = _decimal_str(fill.get("quantity"))
    price = _decimal_str(fill.get("price"))
    amount = ""
    try:
        if fill.get("quantity") is not None and fill.get("price") is not None:
            amount = format(
                (Decimal(str(fill["quantity"])) * Decimal(str(fill["price"]))).normalize(), "f"
            )
    except (InvalidOperation, TypeError, ValueError):
        amount = ""
    payload = {
        "approval_id": getattr(approval, "approval_id", None),
        "intent_id": getattr(approval, "intent_id", None),
        "task_id": task_id,
        "run_id": run_id,
        "symbol": symbol,
        "symbol_name": symbol_name,
        "action": getattr(approval, "action", None),
        "notional": getattr(approval, "notional", None),
        "strategy_tag": signal["strategy_tag"] or getattr(approval, "mode", None),
        "fill_quantity": qty,
        "fill_price": price,
        "fill_amount": amount,
        "fill_time": _format_iso_beijing(fill.get("timestamp")),
        "error": error,
    }
    try:
        await send(chat_id, payload, outcome=outcome)
    except Exception:
        logger.exception(
            "approval result card delivery failed approval_id=%s chat_id=%s outcome=%s",
            getattr(approval, "approval_id", None), chat_id, outcome,
        )
        return "send_failed"
    logger.info(
        "approval result card delivered approval_id=%s chat_id=%s outcome=%s run_id=%s",
        getattr(approval, "approval_id", None), chat_id, outcome, run_id,
    )
    return "delivered"


async def _record_delivered_card(
    cycle_run_repository: Any,
    *,
    run_id: str | None,
    content: str,
    target_kind: str,
    status: str,
    mode: str | None = None,
    channel_id: str | None = None,
    chat_id: str | None = None,
    chat_name: str | None = None,
) -> None:
    """Best-effort: record the pushed digest card on the cycle run.

    Feishu channel pushes go straight to the group and otherwise leave NO
    persisted trace (no ``assistant_messages`` row), so the 周期详情 view cannot
    show what was actually pushed. Recording the exact ``content`` + target +
    outcome on ``cycle_runs.details.delivered_cards`` closes that gap — faithful
    for both the deterministic-card render and AI ``prose`` text. Never raises
    into the fire (delivery is already best-effort).
    """
    patch_fn = getattr(cycle_run_repository, "patch_details", None)
    if not run_id or patch_fn is None:
        return
    try:
        await patch_fn(
            run_id,
            {
                "delivered_cards": [
                    {
                        "kind": "digest",
                        "content": content,
                        "mode": mode,
                        "target_kind": target_kind,
                        "channel_id": channel_id,
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "status": status,
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        )
    except Exception:
        logger.warning(
            "trigger delivery: failed to record delivered card run_id=%s target=%s",
            run_id, target_kind, exc_info=True,
        )


async def deliver_trigger_result(
    assistant_service: Any,
    *,
    trigger: Any,
    run_id: str | None,
    cycle_run_repository: Any,
    instrument_catalog_repository: Any = None,
    task_repository: Any = None,
) -> str | None:
    """Push a fired Trigger's cycle digest per ``trigger.delivery_json``.

    Returns a short status string (delivered/suppressed/skipped/forwarded/
    forward_failed/channel_disabled) for log/visibility, or ``None`` when delivery is
    not configured (mode none / no run). Best-effort: a delivery failure never
    propagates into the fire (the cycle already ran + persisted).

    ``instrument_catalog_repository`` / ``task_repository`` enrich the push with
    each stock's display name and the task's name (best-effort, degrade to bare
    code / id on any miss). A push card is the operator-facing surface — it must
    carry 股票名称 + 任务名, not just opaque codes / ids.
    """
    delivery = getattr(trigger, "delivery_json", None)
    if not isinstance(delivery, dict):
        return None
    mode = delivery.get("mode") or "none"
    if mode == "none" or not run_id:
        return None
    if assistant_service is None:
        logger.warning("trigger delivery skipped (no assistant_service) trigger_id=%s", trigger.id)
        return "skipped"

    digest = None
    if cycle_run_repository is not None:
        try:
            digest = await cycle_run_repository.get_by_run_id(run_id)
        except Exception:
            logger.exception("trigger delivery: cycle digest lookup failed run_id=%s", run_id)
            digest = None

    no_signal = delivery.get("no_signal_mode") or "brief"
    if not has_actionable(digest) and no_signal == "silent":
        logger.info(
            "trigger delivery suppressed (no signal, silent) trigger_id=%s run_id=%s",
            trigger.id, run_id,
        )
        return "suppressed"

    # Resolve operator-facing names (任务名 / 股票名称) best-effort BEFORE the
    # renderers run. A miss is "" / omitted — the deterministic renderers fall
    # back to the bare trigger name / symbol code, never raise.
    task_name = await _resolve_task_name(task_repository, getattr(trigger, "task_id", None))
    symbol_names = await _resolve_symbol_names(
        instrument_catalog_repository, _collect_digest_symbols(digest)
    )

    compose_sections: dict[str, str] | None = None
    if mode == "prose":
        compose_result = await _compose_via_agent(
            assistant_service,
            trigger=trigger,
            digest=digest,
            no_signal_mode=no_signal,
            run_id=run_id,
            composer_agent_id=delivery.get("composer_agent_id"),
        )
        content = compose_result.text if compose_result is not None else None
        if compose_result is not None:
            compose_sections = compose_result.sections
        if not content:
            # Compose produced nothing usable — fall back visibly to the
            # deterministic card. brief/full means "always notify", so a fire is
            # never silently dropped just because the LLM step failed.
            logger.error(
                "trigger prose compose unavailable; delivering deterministic card "
                "trigger_id=%s run_id=%s composer_agent_id=%s",
                trigger.id, run_id, delivery.get("composer_agent_id"),
            )
            content = render_trigger_digest(
                trigger, digest, no_signal_mode=no_signal,
                task_name=task_name, symbol_names=symbol_names,
            )
    else:
        content = render_trigger_digest(
            trigger, digest, no_signal_mode=no_signal,
            task_name=task_name, symbol_names=symbol_names,
        )

    target = delivery.get("target") or {}
    kind = target.get("kind")

    if kind == "session":
        session_id = target.get("session_id")
        if not session_id:
            logger.warning(
                "trigger delivery: session target without session_id trigger_id=%s", trigger.id
            )
            return "skipped"
        status, _info = await deliver_assistant_message_to_session(
            assistant_service,
            target_session_id=session_id,
            content=content,
            cron_job_id=trigger.id,
            cron_job_run_id=run_id,
            cron_task_kind="trigger",
            extra_metadata={"trigger_id": trigger.id, "run_id": run_id},
            source="trigger",
        )
        logger.info(
            "trigger delivery session status=%s trigger_id=%s run_id=%s",
            status, trigger.id, run_id,
        )
        return status

    if kind == "channel":
        channel_id = target.get("channel_id")
        chat_id = target.get("chat_id")
        if not chat_id:
            # A channel target without a resolved chat can't address a Feishu group;
            # don't fall back to a bogus receive_id (it would 400 at the API).
            logger.warning(
                "trigger delivery: channel target missing chat_id channel_id=%s trigger_id=%s",
                channel_id, trigger.id,
            )
            return "channel_disabled"
        manager = getattr(assistant_service, "channel_manager", None)
        channel = manager.get(channel_id) if (manager is not None and channel_id) else None
        if channel is None:
            logger.warning(
                "trigger delivery: channel unavailable channel_id=%s trigger_id=%s",
                channel_id, trigger.id,
            )
            return "channel_disabled"
        from doyoutrade.assistant.channels.base import CardContent, TextContent

        outgoing: Any = TextContent(text=content)
        if getattr(channel, "channel_type", "") == "feishu":
            try:
                # Rich, multi-section CardKit 2.0 card built straight from the
                # persisted digest (行情/判断/账户/持仓/本轮动作) — replaces the
                # plain single-markdown-blob card. Falls back visibly to the
                # proven text-dump card on any build error so a fire is never
                # dropped. Prose-mode section bodies ride along as a collapsed
                # 「AI 解读」 panel (advisory, separate from the facts).
                from doyoutrade.assistant.channels.feishu.card.builder import (
                    build_complete_card,
                    build_signal_digest_card,
                )

                trigger_name = (
                    getattr(trigger, "name", "") or getattr(trigger, "id", "") or "trigger"
                )
                outgoing = CardContent(
                    card=build_signal_digest_card(
                        trigger_name=trigger_name,
                        digest=digest,
                        processed_at=_format_processing_time(digest),
                        no_signal_mode=no_signal,
                        prose_mode=(mode == "prose"),
                        narration_sections=compose_sections,
                        run_id=run_id,
                        task_id=getattr(trigger, "task_id", None),
                        task_name=task_name,
                        symbol_names=symbol_names,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "trigger delivery card build failed trigger_id=%s err=%s; text fallback",
                    trigger.id, exc,
                )
                try:
                    from doyoutrade.assistant.channels.feishu.card.builder import build_complete_card

                    outgoing = CardContent(card=build_complete_card(content, show_tool_use=False))
                except Exception:  # noqa: BLE001 — last-resort: plain text
                    logger.warning(
                        "trigger delivery fallback card build also failed trigger_id=%s; "
                        "delivering plain text",
                        trigger.id,
                    )
                    outgoing = TextContent(text=content)
        # Address the concrete group: feishu send() reads feishu_chat_id (group =>
        # receive_id_type=chat_id). Merge any extra caller meta without losing it.
        send_meta = dict(target.get("meta")) if isinstance(target.get("meta"), dict) else {}
        send_meta.setdefault("feishu_chat_id", chat_id)
        send_meta.setdefault("feishu_chat_type", "group")
        try:
            await channel.send(f"trigger-{run_id}", outgoing, send_meta)
            logger.info(
                "trigger delivery channel forwarded channel_id=%s chat_id=%s trigger_id=%s",
                channel_id, chat_id, trigger.id,
            )
            status = "forwarded"
        except Exception:
            logger.exception(
                "trigger delivery channel send failed channel_id=%s chat_id=%s trigger_id=%s",
                channel_id, chat_id, trigger.id,
            )
            status = "forward_failed"
        # Record the EXACT pushed card on the cycle run so 周期详情 can replay it.
        # A Feishu channel push is otherwise sent straight to the group and left
        # no trace queryable by run_id (§错误可见性: don't lose the user-visible
        # output). Best-effort; never overrides the delivery status.
        await _record_delivered_card(
            cycle_run_repository,
            run_id=run_id,
            content=content,
            target_kind="channel",
            status=status,
            mode=mode,
            channel_id=channel_id,
            chat_id=chat_id,
            chat_name=target.get("chat_name"),
        )
        return status

    logger.warning("trigger delivery: unknown target kind=%s trigger_id=%s", kind, trigger.id)
    return "skipped"
