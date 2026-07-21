"""Stable error types exposed by the todo-db public API."""


class TodoDBError(Exception):
    """Base class for expected tracker errors."""


class TodoError(TodoDBError):
    """Raised when a TODO lifecycle or validation rule is violated."""


class ProjectIdentityMismatchError(TodoDBError):
    """Raised before access when a database belongs to another project."""


class SchemaMismatchError(TodoDBError):
    """Raised when the database migration history differs from packaged SQL."""


class AuditIntegrityError(TodoDBError):
    """Raised when the audit chain or an export signature fails verification."""
