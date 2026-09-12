"""Stable error types exposed by the todo-db public API."""


class TodoDBError(Exception):
    """Base class for expected tracker errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TodoError(TodoDBError):
    """Raised when a TODO lifecycle or validation rule is violated."""


E_CLAIM_STALE = "E_CLAIM_STALE"
E_MULTIPLE_CLAIMS = "E_MULTIPLE_CLAIMS"
E_NOTHING_READY = "E_NOTHING_READY"
E_SCHEMA = "E_SCHEMA"
E_STATE = "E_STATE"
E_CONFLICT = "E_CONFLICT"
E_UNKNOWN = "E_UNKNOWN"
E_OFFLINE = "E_OFFLINE"
E_CURSOR_STALE = "E_CURSOR_STALE"
E_ACTIVE_CLAIMS = "E_ACTIVE_CLAIMS"
E_OVERSIZED = "E_OVERSIZED"
E_OUTPUT_TRUNCATED = "E_OUTPUT_TRUNCATED"
E_NO_PRINCIPAL = "E_NO_PRINCIPAL"
E_FINAL_EVIDENCE = "E_FINAL_EVIDENCE"
