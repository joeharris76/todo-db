# ADR 0007: JSON + Git TODO Tracker

- **Status**: Accepted
- **Date**: 2026-09-08

## Context

The SQLite/Turso runtime (`database.py`, `backends.py`, `tracker.py`,
`agent.py`, `audit.py`, `findings.py`, seven SQL migrations, 26/38 MCP
tools) carries planning policy executable code: mandatory work units, scope
gates, verification ladders, findings/deferral triage, workspace
attestation, and a hash-chained audit trail. Measurements on the old
surface: 26 tool schemas cost 3,344 tokens at startup, a 30-task ready
response cost 5,420 tokens, and a single oversized read escaped the
advertised 16 KiB cap (105,817 bytes). The product question is whether the
normal runtime needs a database at all.

## Decision

Replace the normal runtime with canonical JSON state published through Git
on a dedicated state branch (`index.json` + `items/<id>.json`), a compact
authoritative index, cooperative claims, and a small MCP surface.

- **Extend**: priority bands (`critical` … `low`), dependency readiness and
  cycle rejection, one-winner claims with stable worker identity plus
  generation tokens, the MCP-first interface, bounded context, and the
  `{ok, data}` / `{ok, code, error, recovery, kind}` envelope with a 16 KiB
  cap.
- **Supersede**: the hosted-primary runtime and credential machinery, the
  mandatory workflow gates (work units, scope rules, verification
  execution, lint, findings, attestation, planning lint), the broad tool
  profiles, the SQL query engine, the cryptographic event chain, and the
  old runtime audit requirements. Git history supplies ordinary change
  history; historical audit data survives only as an immutable legacy
  artifact from migration.
- **New**: index-owned fields (`title`, `priority`, `status`, `claim`,
  optional `needs`) with detail files that must not duplicate them;
  lifecycle `open/active/blocked/done/dropped` with an explicit
  status/claim relation; fast-forward publication with semantic
  conflict retries, operation IDs, and unknown-outcome handling; offline
  reads from a cached revision with offline mutations refused; and a
  non-destructive migration from the lossless export envelope that
  archives unmapped fields instead of dropping them.

## Consequences

Breaking simplification: no compatibility layer for removed tools, gates,
or SQL semantics. Migration preserves IDs, priorities, statuses,
dependencies, descriptions, and evidence, refuses live-claim cutover, and
documents rollback. Token targets (~300 for five rows, ~1,500 for one
task, ~100 for a mutation ack) are benchmark acceptance targets measured
with `o200k_base` on actual serialized surfaces, not universal guarantees.
