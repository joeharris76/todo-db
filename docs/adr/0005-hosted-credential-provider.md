# ADR 0005: Hosted Credential Provider

- **Status**: Accepted
- **Date**: 2026-08-19
- **Context Item**: `hosted-credential-provider-adr`
- **Relationship to ADR 0004**: additive. This ADR does not amend, supersede,
  or weaken any decision in ADR 0004.

## Context

ADR 0004 removed automatic token minting from the generated wrapper and placed
credential storage and injection outside todo-db: "Password managers, OS
keychains, CI secret stores, and process supervisors own storage and injection."
That boundary is correct. The components it names were never built or connected.

The result shipped in v0.4.2. `resolve_credential()` accepts an explicit
`DatabaseConfig.auth_token` or a `TODO_DB_*` environment variable and otherwise
raises `E_AUTH_MISSING`. Nothing puts a credential into an interactive process,
so every hosted caller — an agent session, a plain shell, a downstream
consumer — fails unless a person exported a token into that process first. The
stated goal of the work was to remove token churn and reauthentication burden
from the operator. Removing per-command minting achieved the first half; with
no provider, the second half regressed.

The gap is a missing component, not a wrong decision. This ADR adds the
component and the boundary it must respect.

## Decisions

### G1: Acceptance criterion

On a machine provisioned once, a new shell or agent session in any
repository runs the repository wrapper against the hosted tracker with
**zero interactive steps**, and reauthentication is required no more often
than the ADR 0004 rotation maximum for that capability.

This criterion is testable and is asserted by
`scripts/hosted_auth_acceptance.sh`. A release that touches credential
resolution is not usable until that harness passes with the environment
credentials removed.

**Non-goal.** No agent receives ambient read-write tracker authority. A
credential is resolved for the capability the operation requires and is passed
to the `todo-db` child process only. "Usable from every agent" means every agent
reaches the same resolver through the same wrapper, not that every agent holds a
standing write credential.

### G2: Mechanism — a retrieval hook, not a built-in secret store

todo-db reads `TODO_DB_CREDENTIAL_COMMAND`, a caller-configured command that
prints one bearer token to standard output. Any secret store the operator
already trusts can satisfy it: the macOS Keychain via `security
find-generic-password`, 1Password via `op read`, `pass`, `gopass`, a CI secret
helper, or a process supervisor's own fetch command.

Rejected alternatives:

- **A first-party OS keychain reader.** It would add per-platform code and a
  storage opinion to a tool that ADR 0004 deliberately keeps out of secret
  storage, and it would serve exactly one operating system today. The runbook
  documents the Keychain commands instead, which is the same convenience with
  none of the coupling.
- **Both a built-in reader and the hook.** Two resolution paths for the same
  credential doubles the failure surface and the documentation, and the hook
  already expresses everything the reader would.
- **A background refresh daemon or on-disk token cache.** ADR 0004 forbids
  todo-db holding credentials, and a cache outlives the rotation window it was
  minted under.

### G3: Precedence

Resolution order, first match wins:

1. `DatabaseConfig.auth_token` — an explicit programmatic credential.
2. The environment variable for the requested capability:
   `TODO_DB_AUTH_TOKEN` for read-write; `TODO_DB_RO_AUTH_TOKEN` for read-only,
   falling back to `TODO_DB_AUTH_TOKEN` only when the read-only variable is
   absent or empty.
3. `TODO_DB_CREDENTIAL_COMMAND`, asked for the requested capability.
4. `HostedAuthError` with code `E_AUTH_MISSING`.

Environment variables remain the portable CI interface and the deliberate
override; the provider becomes the interactive default. The provider never
displaces a credential that was supplied explicitly, so a CI run behaves
identically whether or not a provider happens to be configured on the machine.

With `TODO_DB_CREDENTIAL_COMMAND` unset, resolution, message text, error codes,
and exit statuses are exactly what v0.4.2 produced.

### G4: Provider protocol

The value is parsed with `shlex.split` and executed directly. It is never passed
to a shell.

- **Capability.** The requested capability, `read-only` or `read-write`, is
  exported to the child as `TODO_DB_CREDENTIAL_CAPABILITY` and nowhere else.
  The operator's `argv` is passed through exactly as written. Appending the
  capability as a positional argument was tried and rejected: it breaks every
  documented one-line provider, because `security find-generic-password` reads
  a trailing word as the keychain to search and exits 44, and `op read` and
  `pass show` reject the extra argument. A provider that ignores the variable
  and always returns the same token is valid; the operator then owns the
  mismatch.
- **Present.** Exit status 0 with non-empty standard output. The token is the
  output with surrounding whitespace stripped and nothing else removed.
- **Absent.** Exit status 0 with empty standard output. Read-only resolution may
  then ask for `read-write`, mirroring the environment-variable fallback. This
  is the only fallback the provider permits.
- **Error.** Any non-zero exit status, timeout, unparsable command string,
  missing executable, or output larger than the size bound. Resolution stops
  with `E_AUTH_MISSING`. An error is never treated as absence, so a broken
  read-only provider can never escalate the caller to a read-write credential.
- **Bounds.** A short default timeout and an output size limit, both
  conservative. A wedged or interactive secret store must degrade to a coded
  error rather than hang a tracker command.
- **Disclosure.** Provider standard output is the bearer token, and provider
  standard error routinely echoes it back on failure. Neither ever appears in an
  exception, log line, doctor field, or tracker evidence. Failures report the
  provider's `argv[0]` and its exit status, nothing more.
- **Invocation count.** At most once per capability per process. The provider is
  never consulted for a local or standalone SQLite backend.
- **Memoization key.** The configured command string and the requested
  capability, which together cover every input the provider is given: it is
  never told which database it is being asked about. A provider that branches on
  some other environment variable read at call time is therefore memoized across
  a change to that variable within one process. That is a documented limitation,
  not a supported configuration.
- **Output handling.** Captured as bytes. The size bound is enforced on the raw
  bytes before any decode, and decoding replaces malformed sequences rather than
  raising, so no provider output can produce an exception that escapes the
  error contract above.

A caller that filters the environment it passes to `todo-db` must forward
`TODO_DB_CREDENTIAL_COMMAND`, or the provider is silently unreachable for that
caller.

### G5: Provisioning

The operations runbook documents the provisioning and rotation commands.
todo-db ships no provisioning subcommand: minting requires Turso control-plane
authority, and putting that behind a todo-db command reintroduces the coupling
ADR 0004 removed.

This decision does **not** discharge the provisioning work. The runbook must
carry a named, followable one-time procedure and a rotation procedure, and the
exit-4 remediation text must name them and name
`TODO_DB_CREDENTIAL_COMMAND` rather than telling the reader to inject a
credential. A "documentation only" outcome that leaves no procedure is how the
injection half of the v0.4.2 plan disappeared and is not an acceptable close.

### G6: Retrieval is not storage

todo-db retrieves a credential; it does not own one. It still never mints,
caches, persists, or writes a bearer token to disk, configuration, logs, audit
events, or tracker evidence, and it still makes no Turso control-plane call
during a normal command. The retrieved value lives in process memory for the
life of that process and is passed only to the database client.

ADR 0004 stands unamended: its provisioning boundary, capability and lifetime
profiles, rotation and compromise procedures, expiry-diagnostics stop, and
identity boundary are all still in force.

## Consequences

- One-time provisioning replaces per-session token export. Reauthentication
  happens on the rotation schedule, not at every session.
- The credential-handling code path grows by one bounded subprocess call, taken
  only when no credential was supplied and only for hosted backends.
- Every caller that filters environment variables — the Pi adapter's sanitized
  environment is the current example — must forward the provider variable.
- Operators who set nothing see v0.4.2 behavior unchanged, so CI and existing
  automation need no migration.
- The acceptance criterion in G1 becomes a release gate; artifact correctness
  and green CI do not satisfy it.
