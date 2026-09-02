"""Stable error types exposed by the todo-db public API."""


class TodoDBError(Exception):
    """Base class for expected tracker errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TodoError(TodoDBError):
    """Raised when a TODO lifecycle or validation rule is violated."""


class HostedAuthError(TodoDBError):
    """Raised when a hosted backend rejects a connection or sync for authentication reasons."""


class ProjectIdentityMismatchError(TodoDBError):
    """Raised before access when a database belongs to another project."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code or E_IDENTITY)


class SchemaMismatchError(TodoDBError):
    """Raised when the database migration history differs from packaged SQL."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code or E_SCHEMA)


class SchemaBehindError(SchemaMismatchError):
    """Raised when the database is at an older schema version than expected."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code or E_SCHEMA_BEHIND)


class SchemaDivergedError(SchemaMismatchError):
    """Raised when the database migrations have diverged from packaged SQL."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code or E_SCHEMA_DIVERGED)


class AuditIntegrityError(TodoDBError):
    """Raised when the audit chain or an export signature fails verification."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code or E_AUDIT)


E_AUTH_MISSING = "E_AUTH_MISSING"
E_AUTH_REJECTED = "E_AUTH_REJECTED"
E_NO_PROJECT = "E_NO_PROJECT"
E_CLAIM_STALE = "E_CLAIM_STALE"
E_MULTIPLE_CLAIMS = "E_MULTIPLE_CLAIMS"
E_BASE_DIVERGED = "E_BASE_DIVERGED"
E_BASE_UNREACHABLE = "E_BASE_UNREACHABLE"
E_LINT_GATE = "E_LINT_GATE"
E_SCOPE_GATE = "E_SCOPE_GATE"
E_VERIFY_GATE = "E_VERIFY_GATE"
E_NOTHING_READY = "E_NOTHING_READY"
E_SCHEMA = "E_SCHEMA"
E_SCHEMA_BEHIND = "E_SCHEMA_BEHIND"
E_SCHEMA_DIVERGED = "E_SCHEMA_DIVERGED"
E_IDENTITY = "E_IDENTITY"
E_AUDIT = "E_AUDIT"
E_OUTPUT_TRUNCATED = "E_OUTPUT_TRUNCATED"
E_HOSTED = "E_HOSTED"
