from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Mapping

from jinja2 import Environment, StrictUndefined


@dataclass(frozen=True)
class AgentPromptTemplate:
    template_id: str
    name: str
    description: str
    filename: str


_TEMPLATES: tuple[AgentPromptTemplate, ...] = (
    AgentPromptTemplate(
        template_id="main-agent",
        name="Main Agent",
        description="Default DoYouTrade operator agent for strategy, graph, and backtest workflows.",
        filename="main_agent.j2",
    ),
    AgentPromptTemplate(
        template_id="swing-trader",
        name="Swing Trader",
        description="Focus on multi-day trend continuation and disciplined risk.",
        filename="swing_trader.j2",
    ),
    AgentPromptTemplate(
        template_id="event-driven",
        name="Event Driven Analyst",
        description="Trade around catalysts, filings, earnings, and policy events.",
        filename="event_driven.j2",
    ),
    AgentPromptTemplate(
        template_id="research-copilot",
        name="Research Copilot",
        description="Turn rough ideas into structured market research and action plans.",
        filename="research_copilot.j2",
    ),
    AgentPromptTemplate(
        template_id="signal-card-composer",
        name="Signal Card Composer",
        description="Compose-only agent that turns a trigger cycle digest into a fixed-shape Chinese push card. No tools, no skills.",
        filename="signal_card_composer.j2",
    ),
)
_TEMPLATE_MAP: dict[str, AgentPromptTemplate] = {item.template_id: item for item in _TEMPLATES}


def get_prompt_template(template_id: str | None) -> AgentPromptTemplate | None:
    key = str(template_id or "").strip()
    if not key:
        return None
    return _TEMPLATE_MAP.get(key)


def _load_template_source(filename: str) -> str:
    return resources.files(__name__).joinpath(filename).read_text(encoding="utf-8")


def _render_template(filename: str, **context: Any) -> str:
    source = _load_template_source(filename)
    env = Environment(
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    return env.from_string(source).render(**context).strip()


def render_prompt_template(template_id: str, **context: Any) -> str:
    template = get_prompt_template(template_id)
    if template is None:
        raise KeyError(f"unknown prompt template: {template_id}")
    return _render_template(template.filename, **context)


def get_prompt_template_text(template_id: str, **context: Any) -> str:
    return render_prompt_template(template_id, **context)


def resolve_agent_system_prompt(agent: Mapping[str, Any] | None) -> str:
    if not agent:
        return ""
    template_id = str(
        agent.get("prompt_template_id")
        or agent.get("system_prompt_template_id")
        or ""
    ).strip()
    if template_id:
        template = get_prompt_template(template_id)
        if template is not None:
            return render_prompt_template(template_id)
    return str(agent.get("system_prompt") or "").strip()


def render_cron_framing(
    *,
    job: Mapping[str, Any],
    task_kind: str,
    fired_at: str,
    user_request: str,
    target_session_id: str | None,
    pre_data: Any = None,
    no_signal_mode: str = "silent",
) -> str:
    """Render the cron-fire system framing prepended to the LLM prompt.

    ``pre_data`` is JSON-serialised here (with non-ASCII preserved) so the
    Jinja template can dump it verbatim without re-invoking ``tojson`` —
    keeps the template free of filter availability concerns.

    ``no_signal_mode`` selects what the agent does when the cycle completed
    normally but produced no actionable buy/sell/close signal:
      - ``silent`` — reply ``[SILENT]`` so the system skips the push
        (legacy default; preserved for non-signal callers).
      - ``brief`` — push a one-line "no new signal" note.
      - ``full`` — push the no-signal note plus an account/position snapshot.
    Callers always pass a concrete value so the template never relies on a
    Jinja ``default`` filter.

    Returns the rendered string (whitespace trimmed)."""

    pre_data_json = (
        json.dumps(pre_data, ensure_ascii=False, indent=2)
        if pre_data is not None
        else None
    )
    return _render_template(
        "cron_framing.j2",
        job=dict(job),
        task_kind=task_kind,
        fired_at=fired_at,
        user_request=user_request,
        target_session_id=target_session_id,
        pre_data=pre_data,
        pre_data_json=pre_data_json,
        no_signal_mode=no_signal_mode,
    )


def render_daily_review_framing(
    *,
    job: Mapping[str, Any],
    fired_at: str,
    asof: str,
    user_request: str,
    target_session_id: str | None,
    statement: Any,
    knowledge: Any,
    metrics: Any = None,
    diagnostics: Any = None,
    market: Any = None,
) -> str:
    """Render the daily-review (每日复盘) cron-fire framing.

    Like :func:`render_cron_framing` it marks a scheduled fire (not a user
    turn) and injects pre-gathered data as a ``<pre_data>`` JSON block, but it
    is *compose-only* (forbids all tool use) and instructs the agent to emit a
    fixed-shape Chinese 复盘 whose first line is ``# <asof> 复盘`` so the
    knowledge-index title stays meaningful. ``statement`` is the live-account
    statement (:func:`doyoutrade.account.statement.gather_account_statement`) and
    ``knowledge`` is the KB digest
    (:func:`doyoutrade.knowledge.review.build_daily_review_knowledge_digest`);
    both are serialised together (non-ASCII preserved) into ``pre_data``.

    Two optional keyword args inject the deterministic analytics layer
    (:mod:`doyoutrade.assistant.review_analytics`) so the LLM cites rather than
    re-derives numbers — mirrors QuantDinger's ``_build_metrics`` /
    ``_build_rule_review`` pre-processing:

      - ``metrics``     — output of :func:`build_review_metrics`. Rendered into
        a ``<pre_metrics>`` block and flagged as authoritative: the agent is
        told **not** to recompute win-rate / fee-rate / concentration from the
        raw statement, only to interpret the why.
      - ``diagnostics`` — output of :func:`build_rule_diagnostics`. Rendered
        into a ``<rule_diagnostics>`` block as deterministic findings the agent
        must cite and may expand on, but never contradict.

    When either is ``None`` (legacy callers, or analytics-layer failure) the
    template degrades gracefully: the corresponding section is omitted and the
    agent is told the raw statement is the only authoritative source.

    ``market`` is the optional 市场四维 (whole-market) block gathered by the
    daily-review executor: the day's 情绪 breadth + 连板梯队, the top 主线 题材
    (concept-board heat), and any 龙虎榜 hits among the user's holdings. It is
    rendered into a ``## 今日市场（大盘/情绪/主线）`` section so the review reads
    as 淘股吧-style 大盘/情绪/主线/个股 four-dimensional context rather than only
    the user's own P&L. It is ``None`` when the (rate-limited, best-effort)
    market gather failed or produced nothing — the section then says so instead
    of fabricating market data.
    """
    pre_data: dict[str, Any] = {
        "account_statement": statement,
        "knowledge": knowledge,
    }
    if market is not None:
        pre_data["market"] = market
    pre_data_json = json.dumps(
        pre_data, ensure_ascii=False, indent=2, default=str
    )
    metrics_json = (
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str)
        if metrics is not None
        else None
    )
    diagnostics_json = (
        json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str)
        if diagnostics is not None
        else None
    )
    return _render_template(
        "daily_review_framing.j2",
        job=dict(job),
        fired_at=fired_at,
        asof=asof,
        user_request=user_request,
        target_session_id=target_session_id,
        pre_data_json=pre_data_json,
        metrics_json=metrics_json,
        diagnostics_json=diagnostics_json,
        has_market=market is not None,
    )


def render_trigger_framing(
    *,
    trigger_name: str,
    trigger_id: str,
    fired_at: str,
    processed_at: str = "",
    run_mode: str,
    digest: Any,
    no_signal_mode: str = "brief",
) -> str:
    """Render the framing that turns a Trigger's cycle digest into a push message.

    Unlike :func:`render_cron_framing` (which is anchored on the user's original
    request phrase), a Trigger fire has no user turn — the whole input is the
    cycle ``digest``. We JSON-serialise it here (non-ASCII preserved) so the
    template dumps it verbatim, and the LLM composes one concise Chinese push.

    The framing is *compose-only*: it explicitly forbids tool use, so the
    composer agent narrates strictly from the provided data (no stock lookups,
    no orders, no recursive cron) — see CLAUDE.md §错误可见性 / recursive-cron-guard.

    ``no_signal_mode`` only switches how verbose the no-signal branch is
    (``brief`` vs ``full``); ``silent`` never reaches here because delivery
    suppresses a no-signal fire before composing.
    """
    digest_json = json.dumps(digest or {}, ensure_ascii=False, indent=2)
    return _render_template(
        "trigger_digest_framing.j2",
        trigger_name=trigger_name,
        trigger_id=trigger_id,
        fired_at=fired_at,
        processed_at=processed_at or "",
        run_mode=run_mode or "—",
        digest_json=digest_json,
        no_signal_mode=no_signal_mode,
    )


def render_approval_framing(*, order: Any) -> str:
    """Render the compose-only framing that narrates a pending LIVE order approval.

    ``order`` is the approval card payload (symbol / symbol_name / action /
    notional / price_reference / order_type / tif / last_price / pct_change /
    direction / strategy_tag / signal_tag / rationale). Like
    :func:`render_trigger_framing` it is compose-only (no tools), so the stock
    NAME and 行情 must already be in ``order`` — the agent narrates strictly from
    it. The composed text becomes the body of the Feishu approval card; the
    approve/reject buttons are appended by the card builder.
    """
    order_json = json.dumps(order or {}, ensure_ascii=False, indent=2)
    return _render_template("approval_card_framing.j2", order_json=order_json)


def render_job_completed_framing(
    *,
    job_id: str,
    job_kind: str,
    job_status: str,
    origin_session_id: str,
    watch_created_at: str,
    note: str | None = None,
) -> str:
    """Render the wake-up framing for a completed background job watch.

    The composer agent (a fresh worker session, mirroring
    :func:`render_cron_framing`'s delivery model) reads the job report via
    CLI and writes the push text; ``JobWatchService`` delivers it into the
    originating session.
    """
    return _render_template(
        "job_completed_framing.j2",
        job_id=job_id,
        job_kind=job_kind,
        job_status=job_status,
        origin_session_id=origin_session_id,
        watch_created_at=watch_created_at,
        note=(note or "").strip() or None,
    )


def list_prompt_templates() -> list[dict[str, str]]:
    return [
        {
            "template_id": item.template_id,
            "name": item.name,
            "description": item.description,
            "system_prompt": get_prompt_template_text(item.template_id),
        }
        for item in _TEMPLATES
    ]


__all__ = [
    "AgentPromptTemplate",
    "get_prompt_template_text",
    "get_prompt_template",
    "list_prompt_templates",
    "render_cron_framing",
    "render_daily_review_framing",
    "render_trigger_framing",
    "render_prompt_template",
    "resolve_agent_system_prompt",
]
