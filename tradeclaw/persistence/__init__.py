"""Persistence modules for async storage and trace records."""

from tradeclaw.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError
from tradeclaw.persistence.models import (
    AgentInstance,
    ApprovalRecord,
    SystemStateRecord,
    TraceEventRecord,
)
from tradeclaw.persistence.repositories import (
    ApprovalSnapshot,
    InstanceSnapshot,
    SqlAlchemyApprovalRepository,
    SqlAlchemyInstanceRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTraceEventRepository,
    TraceEventSnapshot,
)

__all__ = [
    "AgentInstance",
    "ApprovalRecord",
    "ApprovalSnapshot",
    "Base",
    "InstanceSnapshot",
    "PersistenceError",
    "RecordNotFoundError",
    "SqlAlchemyApprovalRepository",
    "SqlAlchemyInstanceRepository",
    "SqlAlchemySystemStateRepository",
    "SqlAlchemyTraceEventRepository",
    "StateConflictError",
    "SystemStateRecord",
    "TraceEventRecord",
    "TraceEventSnapshot",
    "create_engine_and_session_factory",
    "dispose_engine",
]
