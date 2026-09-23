# ADR 0009: Time-held readiness

- **Status**: Accepted
- **Date**: 2026-09-23
- **Relationship to prior ADRs**: extends the storage contract in ADR 0007
  without changing its schema version.

## Context

Some open tasks have nothing to do until a set time. A merged change may need
a 7- or 14-day measurement window before it can close. The ready queue lists
such tasks as ready, so an agent can take and finish one before its window
ends. The only workaround is `blocked`, which mixes waiting tasks in with
stalled ones and needs someone to reopen each task by hand.

## Decision

1. **A field, not a status.** An optional `not_before` timestamp holds an
   `open` task out of the ready queue until that time. The task becomes ready
   again without anyone acting. A new status would touch every status path and
   would still need a manual move back.
2. **Stored in the detail file, with no schema bump.** `items/<id>.json`
   carries `not_before`. Detail validation rejects only index-owned keys and
   type-checks the keys it knows, so older clients load the field, ignore it,
   and keep it when they write. This follows the precedent set by `sessions`.
   - The index was rejected: `ENTRY_KEYS` is exact, so every older reader
     would fail on the new key.
   - A lazy bump to schema 4 when the first hold is written was rejected:
     every older client would then fail to read the whole tracker, which
     costs more than a missed hold.
   - Consequence: an older client does not enforce the hold. It lists and
     takes a held task exactly as before this change. Upgrade every client
     with access to the state branch before relying on holds.
3. **Format.** Input is RFC 3339 with `Z` or an explicit offset. Naive times
   are rejected because writers may be in different zones. A time that is
   not in the future is rejected on create and update as a likely mistake.
   The stored form is `YYYY-MM-DDTHH:MM:SSZ`, matching `claim.expires_at`.
   Stored values are never re-checked against the clock on load, because
   every hold eventually passes.
4. **take refuses a held task.** It returns the gate `E_NOTHING_READY`,
   naming the hold time and how to override it. Taking and finishing early
   would close the task before its wait ends, which is the failure the field
   exists to prevent. The check sits in the service `take`, beside the
   batch-readiness refusal; `op_take` stays readiness-agnostic. `blocked`
   tasks remain takeable because they carry no time contract.
5. **Clearing.** `not_before=""` clears the hold, as an empty string clears
   the other detail fields. MCP tools treat `None` as "not given", so null
   cannot mean clear. finish and drop leave the field alone. Readers report
   a wait only for an `open` task whose time is still ahead, so a leftover
   value on a closed task is inert.
6. **Display.** `list_items` rows and `show_item` carry `waiting_until` only
   while the hold applies.
7. **Unlock counts are unchanged.** A held task is non-terminal and still
   holds back tasks that need it.

## Known limitations

- A `ready_only` page cursor is tied to the state revision, not the clock.
  A hold that ends between two page reads can shift page offsets. Claim
  expiry already behaves this way.
- Nothing notifies anyone when a hold ends. The task reappears in the ready
  queue for the next agent that looks.
