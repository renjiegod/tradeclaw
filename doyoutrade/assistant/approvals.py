"""Blocking tool-call approvals (WireHookHandle-style future pattern).

When a tool call matches an :class:`ApprovalRule`, the dispatch loop creates
an :class:`ApprovalRequest` (a pending ``asyncio.Future``) and suspends the
tool *inside its execution slot* — the existing abort race still cancels it
cleanly. Channels surface the request (Feishu interactive card, web SSE
banner); a button click resolves the future through
:class:`ApprovalBroker.resolve` and the tool either runs or returns a
structured ``approval_rejected`` / ``approval_timeout`` error the model can
react to.

``approve_always`` remembers the rule for the session in
``session.config["approval_allowlist"]``; later matches auto-approve with a
visible ``approval.auto_approved`` event — never silently.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from doyoutrade.observability import get_logger

logger = get_logger(__name__)

ApprovalAction = Literal["approve_once", "approve_always", "reject"]

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0

APPROVAL_ALLOWLIST_CONFIG_KEY = "approval_allowlist"


@dataclass(frozen=True)
class ApprovalRule:
    """One gating rule.

    ``tool`` is the tool name to match. ``command_pattern`` (regex,
    ``re.search``) further narrows ``execute_bash``-style calls by their
    ``command`` argument; ``None`` gates every call of the tool. ``key`` is
    the stable identifier the session allowlist remembers.
    """

    key: str
    tool: str
    description: str
    command_pattern: str | None = None
    timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS

    def matches(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        if tool_name != self.tool:
            return False
        if self.command_pattern is None:
            return True
        command = str(arguments.get("command") or "")
        return re.search(self.command_pattern, command) is not None


# Default high-risk surface: the real-money paths an operator would want a
# second pair of eyes on. Overridable via AssistantService(approval_rules=...).
DEFAULT_APPROVAL_RULES: tuple[ApprovalRule, ...] = (
    ApprovalRule(
        key="strategy_promote",
        tool="execute_bash",
        command_pattern=r"\bstrategy\s+promote\b",
        description="把策略定义提升 / 绑定到任务（可能进入实盘）",
    ),
    ApprovalRule(
        key="task_start",
        tool="execute_bash",
        command_pattern=r"\btask\s+start\b",
        description="启动交易任务",
    ),
    ApprovalRule(
        key="task_stop",
        tool="execute_bash",
        command_pattern=r"\btask\s+stop\b",
        description="停止交易任务",
    ),
    ApprovalRule(
        key="task_delete",
        tool="execute_bash",
        command_pattern=r"\btask\s+delete\b",
        description="删除交易任务",
    ),
    ApprovalRule(
        key="trigger_trade_intent",
        tool="execute_bash",
        command_pattern=r"\btask\s+trigger\s+add\b[^\n]*--intent[= ]trade\b",
        description="添加会真实下单的 trade trigger",
    ),
    ApprovalRule(
        key="account_write",
        tool="execute_bash",
        command_pattern=r"\baccount\s+(create|update|delete|set-default)\b",
        description="修改交易账户配置",
    ),
)


def match_approval_rule(
    rules: tuple[ApprovalRule, ...] | list[ApprovalRule],
    tool_name: str,
    arguments: dict[str, Any],
) -> ApprovalRule | None:
    for rule in rules:
        if rule.matches(tool_name, dict(arguments or {})):
            return rule
    return None


@dataclass(frozen=True)
class ApprovalResolution:
    action: ApprovalAction | Literal["timeout"]
    source: str  # "feishu_card" | "web" | "timeout" | tests
    resolver_id: str = ""
    reason: str = ""


@dataclass
class ApprovalRequest:
    """A pending approval — the WireHookHandle of this implementation."""

    approval_id: str
    session_id: str
    attempt_id: str
    run_id: str
    tool_name: str
    rule_key: str
    description: str
    command_preview: str
    timeout_seconds: float
    created_at: str
    _future: asyncio.Future[ApprovalResolution] = field(repr=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()

    async def wait(self) -> ApprovalResolution:
        """Suspend until resolved; a timeout resolves to action="timeout"."""
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._future), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            resolution = ApprovalResolution(action="timeout", source="timeout")
            if not self._future.done():
                self._future.set_result(resolution)
            return self._future.result()

    def resolve(self, resolution: ApprovalResolution) -> bool:
        """Complete the future; False when already resolved / timed out."""
        if self._future.done():
            return False
        self._future.set_result(resolution)
        return True

    def payload(self) -> dict[str, Any]:
        """Serializable view for events / cards / SSE."""
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "tool": self.tool_name,
            "rule_key": self.rule_key,
            "description": self.description,
            "command_preview": self.command_preview,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
        }


class ApprovalBroker:
    """In-memory registry of pending approvals for one service process.

    Pending approvals do not survive a restart by design: the turn that
    awaits them dies with the process, so persisted rows would be orphans.
    Audit happens through ``approval.*`` session events, which ARE persisted.
    """

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}

    def create(
        self,
        *,
        session_id: str,
        attempt_id: str,
        run_id: str,
        tool_name: str,
        rule: ApprovalRule,
        command_preview: str,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            approval_id=f"appr-{uuid4().hex[:12]}",
            session_id=session_id,
            attempt_id=attempt_id,
            run_id=run_id,
            tool_name=tool_name,
            rule_key=rule.key,
            description=rule.description,
            command_preview=command_preview[:500],
            timeout_seconds=rule.timeout_seconds,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending[request.approval_id] = request
        return request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._pending.get(approval_id)

    def list_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            request.payload()
            for request in self._pending.values()
            if not request._future.done()
            and (session_id is None or request.session_id == session_id)
        ]

    def resolve(
        self,
        approval_id: str,
        *,
        action: ApprovalAction,
        source: str,
        resolver_id: str = "",
        reason: str = "",
    ) -> bool:
        """Resolve a pending approval. False = unknown / already resolved.

        Callers (card handler, web endpoint) must surface a False return to
        the clicker — the request may have timed out or been decided on
        another surface.
        """
        request = self._pending.get(approval_id)
        if request is None:
            logger.info("approval resolve ignored (unknown id) approval_id=%s", approval_id)
            return False
        accepted = request.resolve(
            ApprovalResolution(
                action=action, source=source, resolver_id=resolver_id, reason=reason
            )
        )
        if not accepted:
            logger.info(
                "approval resolve ignored (already resolved) approval_id=%s action=%s source=%s",
                approval_id,
                action,
                source,
            )
        return accepted

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)
