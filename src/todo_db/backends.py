"""Local SQLite and optional Turso/libSQL connection adapters."""

from __future__ import annotations

import logging
import os
import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import E_AUTH_MISSING, E_AUTH_REJECTED, HostedAuthError, TodoDBError
from .models import CredentialMode, DatabaseConfig


LOG = logging.getLogger("todo_db.backends")


def connect(
    config: DatabaseConfig, *, credential: "ResolvedCredential | None" = None
) -> sqlite3.Connection | "HostedConnection":
    if config.is_hosted:
        return _connect_hosted(config, credential=credential)
    return _connect_sqlite(config)


def _connect_sqlite(config: DatabaseConfig) -> sqlite3.Connection:
    path = Path(config.path)
    if config.credential_mode is CredentialMode.READ_ONLY:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=config.timeout)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=config.timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class HostedRow:
    __slots__ = ("_names", "_values")

    def __init__(self, names: list[str], values: tuple[Any, ...]):
        self._names = [name.lower() for name in names]
        self._values = values

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key.lower())]
            except ValueError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class HostedCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    def _row(self, values: Any) -> HostedRow | None:
        if values is None:
            return None
        names = [column[0] for column in (self._cursor.description or [])]
        return HostedRow(names, tuple(values))

    def fetchone(self) -> HostedRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[HostedRow]:
        return [row for value in self._cursor.fetchall() if (row := self._row(value)) is not None]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    token: str = field(repr=False)
    source: str
    capability: str


class HostedConnection:
    """Small sqlite3-compatible surface over the libsql Python client."""

    def __init__(
        self,
        raw: Any,
        *,
        url: str = "",
        credential: ResolvedCredential | None = None,
        token: str = "",
        token_variable: str = "TODO_DB_AUTH_TOKEN",
    ):
        self._raw = raw
        self._url = url
        self._credential = credential or ResolvedCredential(token, token_variable, "unknown")
        self._token = self._credential.token

    def _wrap_error(self, exc: BaseException, context: str) -> Exception:
        if isinstance(exc, TodoDBError):
            return exc
        message = str(exc)
        if isinstance(exc, sqlite3.IntegrityError) or "constraint" in message.lower():
            if self._url:
                message = _redacted_error(exc, url=self._url, token=self._token)
            return sqlite3.IntegrityError(message)
        if self._url:
            return hosted_error(exc, url=self._url, credential=self._credential, context=context)
        if isinstance(exc, sqlite3.OperationalError):
            return exc
        return sqlite3.OperationalError(message)

    def execute(self, sql: str, params=()) -> HostedCursor:
        try:
            return HostedCursor(self._raw.execute(sql, tuple(params)))
        except ValueError as exc:
            raise self._wrap_error(exc, "execute") from None
        except Exception as exc:
            raise self._wrap_error(exc, "execute") from None

    def commit(self) -> None:
        try:
            self._raw.commit()
        except Exception as exc:
            raise self._wrap_error(exc, "commit") from None

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception as exc:
            raise self._wrap_error(exc, "rollback") from None

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception as exc:
            raise self._wrap_error(exc, "close") from None


def _normalize_url(value: str) -> str:
    value = value.strip()
    scheme, separator, rest = value.partition("://")
    return scheme.lower() + separator + rest if separator else value


def _secure_url(value: str) -> str:
    """Accept only transports the driver opens over an encrypted connection.

    An allowlist, not a blocklist. `http://` and `ws://` would put the bearer
    token on the wire in cleartext. `wss://` is refused for a different reason:
    libsql treats any scheme outside libsql/http/https as a local path, so it
    would silently open a file named after the URL rather than connect.
    """

    value = _normalize_url(value)
    if value.startswith(DatabaseConfig.SECURE_SCHEMES):
        return value
    scheme = value.partition("://")[0] or value
    supported = ", ".join(s.rstrip(":/") + "://" for s in DatabaseConfig.SECURE_SCHEMES)
    raise TodoDBError(f"refusing {scheme}:// for the hosted backend; use {supported}")


CREDENTIAL_COMMAND_VARIABLE = "TODO_DB_CREDENTIAL_COMMAND"
CREDENTIAL_COMMAND_TIMEOUT_SECONDS = 5.0
CREDENTIAL_COMMAND_MAX_BYTES = 8192


class _ProviderError(Exception):
    """Internal: the provider ran but could not be trusted to answer.

    Carries only non-secret detail. Provider stdout is the bearer token and
    provider stderr routinely echoes it back, so neither is ever recorded.
    """


_PROVIDER_CACHE: dict[tuple[str, str], ResolvedCredential | None] = {}


def reset_credential_provider_cache() -> None:
    """Forget provider answers resolved earlier in this process. Test and embedding hook."""

    _PROVIDER_CACHE.clear()


def _provider_credential(capability: str) -> ResolvedCredential | None:
    """Ask the configured provider for one capability, per ADR 0005 G4.

    Returns a credential when the provider exits 0 with output, None when it
    exits 0 with no output (the credential is absent and capability fallback
    may continue), and raises _ProviderError for every other outcome so a
    broken provider can never be mistaken for an absent credential.
    """

    configured = os.environ.get(CREDENTIAL_COMMAND_VARIABLE, "").strip()
    if not configured:
        return None
    cache_key = (configured, capability)
    if cache_key in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[cache_key]
    try:
        argv = shlex.split(configured)
    except ValueError as exc:
        raise _ProviderError(f"{CREDENTIAL_COMMAND_VARIABLE} is not a parsable command line") from exc
    if not argv:
        raise _ProviderError(f"{CREDENTIAL_COMMAND_VARIABLE} is empty after parsing")

    # The capability travels only in the child environment. Appending it as a
    # positional argument would break every documented one-line provider:
    # `security find-generic-password -w -s <service>` reads a trailing word as
    # the keychain to search and exits 44, and `op read` and `pass show` reject
    # the extra argument the same way. A provider that needs to branch reads
    # TODO_DB_CREDENTIAL_CAPABILITY; a plain retrieval command needs nothing.
    child_env = dict(os.environ)
    child_env["TODO_DB_CREDENTIAL_CAPABILITY"] = capability
    program = argv[0]
    try:
        # argv list, never a shell: the configured string is operator-owned but
        # must not become an injection surface. Output is captured as bytes so
        # that no decode can raise from inside communicate() and escape the
        # error handling below.
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=CREDENTIAL_COMMAND_TIMEOUT_SECONDS,
            env=child_env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _ProviderError(f"credential provider {program!r} was not found") from exc
    except PermissionError as exc:
        raise _ProviderError(f"credential provider {program!r} is not executable") from exc
    except subprocess.TimeoutExpired as exc:
        raise _ProviderError(
            f"credential provider {program!r} exceeded "
            f"{CREDENTIAL_COMMAND_TIMEOUT_SECONDS:g}s"
        ) from exc
    except OSError as exc:
        raise _ProviderError(f"credential provider {program!r} could not be started") from exc

    if completed.returncode != 0:
        raise _ProviderError(f"credential provider {program!r} exited {completed.returncode}")
    raw = completed.stdout or b""
    if len(raw) > CREDENTIAL_COMMAND_MAX_BYTES:
        raise _ProviderError(
            f"credential provider {program!r} returned more than "
            f"{CREDENTIAL_COMMAND_MAX_BYTES} bytes"
        )
    token = raw.decode("utf-8", "replace").strip()
    # The capability is what was asked for, not a property of what came back: a
    # single-entry provider may ignore the request and return one token for
    # both. Label it so no output claims a capability nothing verified.
    resolved = (
        ResolvedCredential(token, CREDENTIAL_COMMAND_VARIABLE, f"requested:{capability}")
        if token
        else None
    )
    _PROVIDER_CACHE[cache_key] = resolved
    return resolved


def _provider_or_missing(capabilities: tuple[str, ...], missing_message: str) -> ResolvedCredential:
    """Try each capability in order, then raise the caller's missing-credential error.

    With no provider configured the message is exactly what it was before the
    provider existed, so callers that never adopt one see no change at all.
    """

    configured = os.environ.get(CREDENTIAL_COMMAND_VARIABLE, "").strip()
    try:
        for capability in capabilities:
            credential = _provider_credential(capability)
            if credential is not None:
                return credential
    except _ProviderError as exc:
        raise HostedAuthError(str(exc), code=E_AUTH_MISSING) from None
    if configured:
        missing_message = f"{missing_message} ({CREDENTIAL_COMMAND_VARIABLE} returned no credential)"
    raise HostedAuthError(missing_message, code=E_AUTH_MISSING)


def resolve_credential(config: DatabaseConfig) -> ResolvedCredential:
    """Resolve one hosted credential and retain only non-secret provenance metadata."""

    if config.auth_token:
        return ResolvedCredential(config.auth_token, "DatabaseConfig.auth_token", "unknown")
    if config.credential_mode is CredentialMode.READ_WRITE:
        token = os.environ.get("TODO_DB_AUTH_TOKEN", "")
        if token:
            return ResolvedCredential(token, "TODO_DB_AUTH_TOKEN", "read-write")
        return _provider_or_missing(
            ("read-write",),
            "hosted backend requires a read-write credential: set TODO_DB_AUTH_TOKEN, or "
            "provision one into your secret store and point TODO_DB_CREDENTIAL_COMMAND at it "
            "(docs/operations/hosted-credentials.md, Provision once)",
        )

    read_only = os.environ.get("TODO_DB_RO_AUTH_TOKEN", "")
    if read_only:
        return ResolvedCredential(read_only, "TODO_DB_RO_AUTH_TOKEN", "read-only")
    read_write = os.environ.get("TODO_DB_AUTH_TOKEN", "")
    if read_write:
        return ResolvedCredential(read_write, "TODO_DB_AUTH_TOKEN", "read-write")
    return _provider_or_missing(
        ("read-only", "read-write"),
        "hosted read access requires a credential: set TODO_DB_RO_AUTH_TOKEN or "
        "TODO_DB_AUTH_TOKEN, or provision one into your secret store and point "
        "TODO_DB_CREDENTIAL_COMMAND at it "
        "(docs/operations/hosted-credentials.md, Provision once)",
    )


def _url_redaction_variants(url: str) -> set[str]:
    if not url:
        return set()
    normalized = _normalize_url(url)
    parsed = urlsplit(normalized)
    variants = {url, normalized}
    if parsed.netloc:
        variants.add(parsed.netloc)
    if parsed.hostname:
        variants.add(parsed.hostname)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            variants.add(f"{parsed.hostname}:{port}")
        elif parsed.scheme in {"libsql", "https"}:
            variants.add(f"{parsed.hostname}:443")
    if parsed.scheme == "libsql":
        variants.add(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
    return {variant for variant in variants if variant}


def _redacted_error(exc: BaseException, *, url: str, token: str) -> str:
    message = str(exc)
    for variant in sorted(_url_redaction_variants(url), key=len, reverse=True):
        message = message.replace(variant, "[REDACTED]")
    return message.replace(token, "[REDACTED]") if token else message


# High-confidence auth evidence only. Ambiguous failures -- quota, suspension,
# network, TLS -- must stay generic: a caller that reads them as an auth failure
# would rotate or mint a credential in response to an unrelated outage. Patterns
# are anchored on the credential noun so that unrelated text merely containing
# "invalid" or "token" (a JSON parse error, a missing request field) does not
# match.
_AUTH_MARKERS = re.compile(
    "|".join(
        (
            r"\b401\b",
            r"\bunauthorized\b",
            r"\bforbidden\b",
            # "authentication failed" / "authentication error"
            r"\bauthentication\s+(?:failed|error)\b",
            # concatenated forms such as "InvalidToken"
            r"\binvalid[ _-]?token\b",
            # "<adjective> auth/access/api token", never a bare "token"
            r"\b(?:expired|rejected|invalid|revoked)\s+(?:auth\w*|access|api|bearer|db|database)"
            r"[ _-]?tokens?\b",
            # "auth token <adjective>"
            r"\b(?:auth\w*|access|api|bearer|db|database)[ _-]?tokens?\s+(?:is\s+|was\s+|has\s+)?"
            r"(?:expired|rejected|invalid|revoked)\b",
            # credentials, in either order -- the noun is unambiguous on its own
            r"\bcredentials?\s+(?:is\s+|are\s+|was\s+|were\s+|has\s+|have\s+)?"
            r"(?:expired|rejected|invalid|revoked)\b",
            r"\b(?:expired|rejected|invalid|revoked)\s+credentials?\b",
            # jwt, in either order
            r"\bjwt\b[^.;\n]{0,40}?\b(?:expired|invalid)\b",
            r"\b(?:expired|invalid)\b[^.;\n]{0,40}?\bjwt\b",
        )
    ),
    re.IGNORECASE,
)


def is_auth_shaped(detail: str) -> bool:
    """Best-effort auth classification requiring high-confidence upstream evidence."""

    return _AUTH_MARKERS.search(detail) is not None


def auth_remediation(credential: ResolvedCredential | None = None) -> str:
    source = credential.source if credential is not None else "the configured credential source"
    return (
        f"credential rejected: replace the bounded credential from {source} and retry in a fresh "
        "process (docs/operations/hosted-credentials.md, Rotate: routine replacement)"
    )


def hosted_error(
    exc: BaseException, *, url: str, credential: ResolvedCredential, context: str
) -> TodoDBError:
    """Redact and classify a hosted failure: HostedAuthError when auth-shaped, TodoDBError otherwise."""

    detail = _redacted_error(exc, url=url, token=credential.token)
    if is_auth_shaped(detail):
        return HostedAuthError(
            f"hosted backend {context} failed: {detail}; {auth_remediation(credential)}",
            code=E_AUTH_REJECTED,
        )
    return TodoDBError(f"hosted backend {context} failed: {detail}")


def _connect_hosted(
    config: DatabaseConfig, *, credential: ResolvedCredential | None = None
) -> HostedConnection:
    url = _secure_url(str(config.path))
    credential = credential or resolve_credential(config)
    token = credential.token
    try:
        import libsql
    except ImportError as exc:
        raise TodoDBError("hosted backend requires the `todo-db[hosted]` extra") from exc

    try:
        raw = libsql.connect(url, auth_token=token, isolation_level=None)
    except Exception as exc:
        raise hosted_error(exc, url=url, credential=credential, context="connection") from None
    connection = HostedConnection(raw, url=url, credential=credential)
    if config.credential_mode is not CredentialMode.READ_ONLY:
        _enable_foreign_keys(connection)
    return connection


def _enable_foreign_keys(connection: "HostedConnection") -> None:
    """Turn on foreign keys for a hosted write connection, and say so if it fails.

    The schema leans on ON DELETE CASCADE (migrations 003/004/005), so a
    connection without enforcement can orphan rows. Not every hosted endpoint
    honours a session PRAGMA over Hrana, and refusing to connect would take a
    working deployment offline, so this warns rather than raising -- but it
    verifies rather than assuming, and it never fails silently.
    """

    warning = (
        "hosted backend cannot confirm foreign-key enforcement (%s); this connection may not "
        "honour ON DELETE CASCADE, so deletes can orphan rows"
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        row = connection.execute("PRAGMA foreign_keys").fetchone()
    except Exception as exc:
        LOG.warning(warning, f"pragma rejected: {type(exc).__name__}")
        return
    # An endpoint that accepts the SET but returns nothing for the GET has not
    # confirmed anything. Treat unproven the same as disabled rather than
    # assuming success, which is how this failed silently before.
    if row is None:
        LOG.warning(warning, "endpoint returned no value for `PRAGMA foreign_keys`")
    elif not row[0]:
        LOG.warning(warning, "still reported disabled after being set")
