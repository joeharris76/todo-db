# ADR 0004: Hosted Credential Lifecycle

- **Status**: Accepted
- **Date**: 2026-08-18
- **Context Item**: `headless-credential-model`

## Context

Hosted todo-db connections use Turso bearer tokens. Turso's token command
defaults to `--expiration never`, while invalidation rotates database signing
keys and invalidates every token in that scope. The existing generated wrapper
also mints a new read-write token after an authentication failure, but can
export it only to its child process. A stale caller therefore repeats the
control-plane request and accumulates unbounded credentials.

The runtime needs predictable data-plane credentials. It must not become a
secret store or depend on a broadly privileged Turso login to repair normal
commands.

## Decisions

### Provisioning boundary

Operators provision tokens outside todo-db and inject them through the existing
`TODO_DB_AUTH_TOKEN`, `TODO_DB_RO_AUTH_TOKEN`, or explicit
`DatabaseConfig.auth_token` boundary. Normal commands, `doctor`, generated
wrappers, and CI jobs do not create, refresh, cache, or persist credentials.
Password managers, OS keychains, CI secret stores, and process supervisors own
storage and injection.

Database-scoped tokens are the supported operating model. Group-scoped tokens
are outside the standard runbook because database-level invalidation may not
revoke them; an operator using group tokens must maintain a separate group
invalidation and redistribution procedure.

### Capability and lifetime profiles

| Consumer | Required capability | Standard maximum lifetime | Rotation owner |
| --- | --- | --- | --- |
| Developer or interactive agent | Read-write | 90 days | Named developer or team owner |
| Headless mutation worker | Read-write | 90 days | Service owner |
| Scheduled audit/export | Read-only | 180 days | Workflow/repository owner |
| Disposable acceptance database | Least capability supported by the test | Database lifetime, with an explicit short expiration preferred | Test runner |

These are maximums, not automatic defaults. Operators may choose shorter
lifetimes. Production read-write tokens must not be non-expiring. A
non-expiring read-only token requires recorded risk acceptance, a named owner,
and an incident-response path.

`CredentialMode.READ_ONLY` is a client selection policy, not a server-side
sandbox. Read-only access is enforced only when `TODO_DB_RO_AUTH_TOKEN` was
actually minted as a bounded read-only credential; the operations runbook gives
the required command.

### Rotation and compromise response

Routine rotation overlaps credentials: mint a bounded replacement before the
old token expires, update consumers, verify them, and let the old token expire.
Do not rotate signing keys merely to replace a healthy token.

Suspected compromise is intentionally disruptive: invalidate the affected
database token scope first, then mint and redistribute every required
replacement. Database invalidation revokes all database-scoped tokens, so the
operator must replace both read-write and read-only credentials. Group tokens,
if any, require separate group invalidation.

### CLI compatibility

Implicit wrapper remediation will be removed. A new wrapper and CLI negotiate a
non-secret v2 authentication exit contract. The marker is only a compatibility
handshake; it does not prove identity, authorization, or token validity. Legacy
callers must not receive the exit code that causes old wrappers to mint tokens.

### Expiry diagnostics: stop

Do not decode JWT claims in todo-db. An unverified `exp` claim is advisory at
best, opaque tokens are valid inputs, and secret stores already know their
rotation schedule. Adding a parser would duplicate lifecycle ownership while
creating misleading output and additional token-handling code.

Rotation owners must monitor issuance and replacement dates externally.
`doctor` continues to test actual server acceptance without printing token
contents. Reconsider only if Turso publishes a stable local metadata contract
and operators demonstrate that external rotation reminders are insufficient.

### Identity boundary

Actor and claim labels remain cooperative attribution, not authentication. ADR
0003 defines that threat boundary. Database credentials authorize backend
access but do not establish an individual actor's identity.

## Consequences

- Runtime database access no longer depends on Turso control-plane login.
- Token compromise has an explicit maximum lifetime and emergency procedure.
- Read-only CI remains least privilege and never receives the read-write
  variable.
- Rotation requires an owner and an external reminder; todo-db does not provide
  a refresh daemon or secret cache.
- Existing wrappers require an explicit migration before the v2 exit contract
  is enabled.
