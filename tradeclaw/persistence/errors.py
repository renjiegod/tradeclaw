class PersistenceError(Exception):
    pass


class RecordNotFoundError(PersistenceError):
    pass


class StateConflictError(PersistenceError):
    pass
