# Hosted credential operations (superseded)

As of 0.7.0 this runbook no longer applies: the Turso/libSQL backends,
their credential machinery (`TODO_DB_AUTH_TOKEN`,
`TODO_DB_RO_AUTH_TOKEN`, `TODO_DB_CREDENTIAL_COMMAND`,
`TODO_DB_AUTH_CONTRACT`), and the provider contract are removed
([ADR 0007](../adr/0007-json-git-tracker.md)). The state branch shares
its repository's access and visibility — there is nothing to mint,
rotate, or scope beyond the repository's own access control. Never
publish credentials to the state branch to compensate.

Kept as a historical record for the 0.6.x line. Do not follow it for
current releases.
