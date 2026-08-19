# Hosted credential operations

This runbook covers database-scoped Turso credentials for todo-db. Keep bearer
tokens out of the repository, command arguments, logs, tracker evidence, and
configuration files.

ADR 0004 sets the lifecycle: capability profiles, lifetime maximums, rotation
ownership, and compromise response. ADR 0005 sets the provider contract that
lets a provisioned credential reach a session without an interactive step.

## Provision

Choose the shortest practical lifetime. These examples use the ADR 0004
maximums:

```sh
# Developer or headless writer: 90 days.
export TODO_DB_AUTH_TOKEN="$(turso db tokens create <database> --expiration 90d)"

# Scheduled audit/export: server-enforced read-only, 180 days.
export TODO_DB_RO_AUTH_TOKEN="$(turso db tokens create <database> --read-only --expiration 180d)"
```

Inject the result with an OS keychain, password manager, process supervisor, or
CI secret store. Do not save it in `.todo-db/config.json` or a wrapper-managed
cache. Record the database, capability, issue date, expiry date, owner, and
replacement reminder in the external secret inventory—never the token value in
tracker evidence.

Do not use group-scoped tokens with this runbook. If an operator deliberately
uses one, the owner must document group-level invalidation and every database
that shares its blast radius.

## Validate

Run a read-only preflight before a batch:

```sh
TODO_DB_RO_AUTH_TOKEN=... todo-db --db libsql://<host> doctor --json
```

A mutation worker validates its read-write credential explicitly:

```sh
TODO_DB_AUTH_TOKEN=... todo-db --db libsql://<host> doctor --rw --json
```

Read-only mode is server-enforced only when the supplied token was created with
`--read-only`. Never place `TODO_DB_AUTH_TOKEN` in the scheduled audit job.

## Routine replacement

1. The named owner mints a bounded replacement before the current expiry.
2. Update one external consumer or secret store.
3. Run `doctor` and the consumer's normal read or mutation smoke check.
4. Update the remaining consumers.
5. Confirm the old token is no longer injected and let it expire naturally.

Do not run database-wide invalidation for routine replacement: it revokes other
healthy credentials in the same database scope.

## Expired or rejected credential

Stop database-dependent work. Mint a bounded replacement outside todo-db,
update the external injector, start a fresh process so it receives the new
environment, and run `doctor` again. A child process cannot repair its parent
shell's environment.

Do not retry a rejected read-only token with a read-write credential. That can
hide an incorrect permission or an accidental write path.

## Suspected compromise

1. Identify whether the token is database- or group-scoped.
2. For a database-scoped token, immediately run:

   ```sh
   turso db tokens invalidate <database> --yes
   ```

3. Assume every database-scoped token is invalid, including read-only tokens.
4. Mint new bounded read-write and read-only credentials as required.
5. Redistribute them through the external secret stores and restart consumers.
6. Run `doctor`, audit verification, and the relevant smoke checks.
7. Record the incident without recording any bearer value.

A group-scoped token requires `turso group tokens invalidate` and a separate
inventory-driven response. Database invalidation alone may not cover it.

## Scheduled audit ownership

The repository/workflow owner maintains `TODO_DB_PROD_RO_TOKEN`, its issue and
expiry dates, and a replacement reminder. The workflow environment must contain
`TODO_DB_RO_AUTH_TOKEN` and must not contain `TODO_DB_AUTH_TOKEN`. CI never
mints credentials and never receives Turso account credentials.

## Acceptance-test exception

A disposable test database is destroyed at the end of the test, but an explicit
short expiration is still preferred as defense in depth. The exception does not
apply to persistent development, staging, or production databases.
