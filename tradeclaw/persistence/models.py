from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tradeclaw.persistence.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentInstance(Base):
    __tablename__ = "instances"

    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    orchestrator_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    watch_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    execution_strategy: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TraceEventRecord(Base):
    __tablename__ = "trace_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_trace_events_run_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class SystemStateRecord(Base):
    __tablename__ = "system_state"

    state_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
