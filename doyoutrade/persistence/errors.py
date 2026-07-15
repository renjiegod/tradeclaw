class PersistenceError(Exception):
    pass


class RecordNotFoundError(PersistenceError):
    pass


class StateConflictError(PersistenceError):
    pass


class AgentInUseError(StateConflictError):
    """Raised when an agent cannot be deleted because rows still reference it."""

    pass


class BuiltinAgentImmutableError(StateConflictError):
    """Raised when a write would mutate the code-fixed builtin main agent.

    The builtin main agent (``id == MAIN_AGENT_ID`` / ``is_builtin=True``) is
    defined in code: it cannot be deleted or renamed, and only a small set of
    runtime knobs (``model_route_name`` / ``context_compaction`` / ``max_turns``)
    may be edited. Any attempt to delete it or to update a locked field raises
    this so the boundary is visible (API maps it to HTTP 403 with
    ``error_code=agent_builtin_immutable``) rather than silently dropped.
    """

    pass
