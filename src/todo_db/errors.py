"""Stable error types exposed by the todo-db public API."""


class TodoDBError(Exception):
    """Base class for expected tracker errors."""


class TodoError(TodoDBError):
    """Raised when a TODO lifecycle or validation rule is violated."""


class HostedAuthError(TodoDBError):
    """Raised when a hosted backend rejects a connection or sync for authentication reasons."""


class ProjectIdentityMismatchError(TodoDBError):
    """Raised before access when a database belongs to another project."""


class SchemaMismatchError(TodoDBError):
    """Raised when the database migration history differs from packaged SQL."""


class SchemaBehindError(SchemaMismatchError):
    """Raised when the database is at an older schema version than expected."""


class SchemaDivergedError(SchemaMismatchError):
    """Raised when the database migrations have diverged from packaged SQL."""


class AuditIntegrityError(TodoDBError):
    """Raised when the audit chain or an export signature fails verification."""


E_NO_PROJECT = "E_NO_PROJECT"
E_CLAIM_STALE = "E_CLAIM_STALE"
E_BASE_DIVERGED = "E_BASE_DIVERGED"
E_BASE_UNREACHABLE = "E_BASE_UNREACHABLE"
E_LINT_GATE = "E_LINT_GATE"
E_SCOPE_GATE = "E_SCOPE_GATE"
E_VERIFY_GATE = "E_VERIFY_GATE"
E_NOTHING_READY = "E_NOTHING_READY"
