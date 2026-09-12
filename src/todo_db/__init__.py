"""Public API for the JSON/Git TODO tracker."""

from importlib.metadata import version

from . import store
from .errors import (
    E_ACTIVE_CLAIMS,
    E_CLAIM_STALE,
    E_CONFLICT,
    E_CURSOR_STALE,
    E_MULTIPLE_CLAIMS,
    E_NOTHING_READY,
    E_NO_PRINCIPAL,
    E_OFFLINE,
    E_OUTPUT_TRUNCATED,
    E_OVERSIZED,
    E_SCHEMA,
    E_STATE,
    E_UNKNOWN,
    TodoDBError,
    TodoError,
)
from .git_backend import ReconcileResult, StateRef, bootstrap, history, mutate, new_op_id, read, reconcile
from .service import TrackerService, count_tokens

TOOL_VERSION = version("todo-db")

__all__ = [
    "E_ACTIVE_CLAIMS",
    "E_CLAIM_STALE",
    "E_CONFLICT",
    "E_CURSOR_STALE",
    "E_MULTIPLE_CLAIMS",
    "E_NOTHING_READY",
    "E_NO_PRINCIPAL",
    "E_OFFLINE",
    "E_OUTPUT_TRUNCATED",
    "E_OVERSIZED",
    "E_SCHEMA",
    "E_STATE",
    "E_UNKNOWN",
    "TOOL_VERSION",
    "ReconcileResult",
    "StateRef",
    "TodoDBError",
    "TodoError",
    "TrackerService",
    "bootstrap",
    "count_tokens",
    "history",
    "mutate",
    "new_op_id",
    "read",
    "reconcile",
    "store",
]
