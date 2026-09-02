# Implement TODOs

## Steps

1. Run `todo ready`, then `todo claim <id>` for the full work order: scope,
   must-preserve notes, anti-patterns, verification, ready units, and deferrals.
   If `ready` warns about findings, run `todo finding candidates` (see
   `references/findings.md`) and triage before choosing new work. Findings are
   not claimable items; use the nine `finding` verbs documented in
   `references/findings.md` and `todo finding --help`.
2. For each work unit: run `todo start <id> <wid>` (optional), apply
   `shared-change-framework/SKILL.md` Section 1 before source-code edits,
   implement the unit, then run `todo done <id> <wid> --evidence "<command or commit or PR>"`.
3. Defer out-of-scope work immediately with
   `todo defer <id> --summary "..." --reason "..."`.
4. Before committing, run `todo check-scope <id>` and
   `todo verify <id> --run <seq>` (SEQ is required; with no flag the command only reports recorded verifications;
   use the concrete sequence number, e.g. `todo verify <id> --run 1`).
5. Resolve every deferral with `todo promote <deferral-id> --to-item <slug>` or
   `todo dismiss <deferral-id> --reason "..."`. Then run
   `todo complete <id> --pr <n>`; it refuses unresolved deferrals.

## Findings

Capture -> land -> triage -> promote/dismiss is the end-to-end pipeline:

- **Capture:** `finding create` writes a credential-free draft under
  `~/.todo-db/finding-drafts/<project-id>/` (or `--drafts-dir`); `candidates` lists unsynced drafts.
- **Land:** `finding sync` is the sole credentialed landing step that validates drafts (including the required body
  headings `## Finding`, `## Why this matters`, `## Suggested next steps` and the `# <title>` heading) and inserts
  them into the tracker.
- **Triage/link/dismiss:** `finding triage`, `link`, `dismiss`, plus `list`/`show`, operate on landed findings.
- **Promote:** `finding promote` atomically promotes a landed finding to a planning item.

See `references/findings.md` for the complete verb table, draft format, and verification. `todo finding --help`
is the contract; findings never enter the ready queue.
