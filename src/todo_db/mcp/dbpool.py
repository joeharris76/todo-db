"""Per-tool capability-scoped credential and connection lifecycle (ADR 0006 G3).

Each MCP tool call resolves a credential for the capability it requires and opens
a fresh connection that is closed when the call returns. Read-only tools never
hold a read-write credential. A write tool never triggers schema migration.

Maps every tool name to a ``CredentialMode`` using the same table the CLI uses
per command (``cli._mode_for``). On ``E_AUTH_REJECTED`` the credential-provider
cache is reset and the credential is re-resolved once, because the CLI's
``retry in a fresh process`` remediation is not an action a model can take.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from ..backends import reset_credential_provider_cache, resolve_credential
from ..database import TodoDatabase
from ..errors import E_AUTH_REJECTED, HostedAuthError, TodoDBError, TodoError
from ..models import CredentialMode
from .target import ResolvedTarget

# Tool → CredentialMode. Mirrors cli._mode_for's AGENT_MUTATING_SUBCOMMANDS etc.
# Read-write tools mutate the tracker; read-only tools do not.
_TOOL_MODES: dict[str, CredentialMode] = {
    # work (hot path)
    "next": CredentialMode.READ_ONLY,
    "take": CredentialMode.READ_WRITE,
    "context": CredentialMode.READ_ONLY,
    "progress": CredentialMode.READ_WRITE,
    "finish": CredentialMode.READ_WRITE,
    "release": CredentialMode.READ_WRITE,
    "adopt": CredentialMode.READ_WRITE,
    "claims": CredentialMode.READ_ONLY,
    # query (default profile, read-only / no-shell)
    "list_items": CredentialMode.READ_ONLY,
    "show_item": CredentialMode.READ_ONLY,
    "ready": CredentialMode.READ_ONLY,
    "stats": CredentialMode.READ_ONLY,
    "deps": CredentialMode.READ_ONLY,
    "deferrals": CredentialMode.READ_ONLY,
    "export": CredentialMode.READ_ONLY,
    "check_scope": CredentialMode.READ_ONLY,
    "verify_list": CredentialMode.READ_ONLY,
    "lint": CredentialMode.READ_ONLY,
    "start_unit": CredentialMode.READ_WRITE,
    # plan (full profile)
    "create_item": CredentialMode.READ_WRITE,
    "update_item": CredentialMode.READ_WRITE,
    "add_dependency": CredentialMode.READ_WRITE,
    "defer": CredentialMode.READ_WRITE,
    "promote_deferral": CredentialMode.READ_WRITE,
    "dismiss_deferral": CredentialMode.READ_WRITE,
    "block": CredentialMode.READ_WRITE,
    "unblock": CredentialMode.READ_WRITE,
    "drop": CredentialMode.READ_WRITE,
    # findings (full profile, capture is file-only not DB)
    "finding_create": CredentialMode.READ_ONLY,
    "finding_list": CredentialMode.READ_ONLY,
    "finding_show": CredentialMode.READ_ONLY,
    "finding_triage": CredentialMode.READ_WRITE,
    "finding_link": CredentialMode.READ_WRITE,
    "finding_promote": CredentialMode.READ_WRITE,
    "finding_dismiss": CredentialMode.READ_WRITE,
    # admin (full profile, read/bootstrap only)
    "init_project": CredentialMode.READ_WRITE,
    "config_get": CredentialMode.READ_ONLY,
    # instructions
    "get_instructions": CredentialMode.READ_ONLY,
}


def mode_for_tool(name: str) -> CredentialMode:
    """Return the CredentialMode for *name*, defaulting to READ_ONLY."""

    return _TOOL_MODES.get(name, CredentialMode.READ_ONLY)


@contextmanager
def database_for_tool(
    target: ResolvedTarget,
    tool: str,
    *,
    allow_hosted: bool = False,
) -> Generator[TodoDatabase, None, None]:
    """Yield a fresh ``TodoDatabase`` for one tool call and close it on return.

    Resolves a capability-scoped credential for *tool* only, opens without
    ``_migrate`` (so a write tool never silently migrates), and runs the
    audit-head check that ``TodoDatabase.open`` performs. On
    ``E_AUTH_REJECTED`` the provider cache is reset and the open is retried
    once.
    """

    if target.is_hosted and not allow_hosted:
        raise TodoError(
            f"refusing hosted target {target.db_target!r} without --allow-hosted",
            code="E_HOSTED",
        )

    mode = mode_for_tool(tool)
    config = target.database_config(mode)
    if config.is_hosted:
        # Prime the provider cache early and surface missing-credential errors
        # before opening; ``TodoDatabase.open`` will re-resolve from cache.
        resolve_credential(config)

    def _open() -> TodoDatabase:
        cfg = target.database_config(mode)
        return TodoDatabase.open(cfg, migrate=False)

    try:
        db = _open()
    except HostedAuthError as exc:
        if exc.code == E_AUTH_REJECTED:
            reset_credential_provider_cache()
            try:
                db = _open()
            except HostedAuthError:
                raise
        else:
            raise
    except (TodoDBError, HostedAuthError):
        raise

    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass
