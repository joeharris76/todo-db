# ADR 0011: Session-state fields

- **Status**: Accepted
- **Date**: 2026-10-17
- **Amends**: ADR 0006 G9 (tool surface), ADR 0007 (JSON/Git tracker)
- **Follows**: ADR 0009 and ADR 0010 (detail-owned, additive fields)

## Context

Agents kept resumable session state in free-form Markdown handoff files.
Those files competed with the tracker, held decisions that later sessions
rewrote or invented, recorded holds as unenforced prose, and grew to
30–90 KB. Projects now keep that state on tracker items instead. An item
needs four things to carry it safely: why it is blocked, what the owner
decided and what is still open, which acceptance criteria are met, and a
bound on how large it can grow. The state branch can live in a public
repository, so writes must also refuse credentials.

## Decision

### 1. Blocked reason

`update_item` takes `reason`. Moving a task to `blocked` requires a
non-empty reason, stored as detail key `blocked_reason` (up to 1000
characters, multi-line, the same character rules as a deferral reason).
Blocking an already-blocked task with a new reason replaces it; moving to
`open` removes it. A `reason` without `status="blocked"` is refused with
`E_STATE`.

`take` refuses a blocked task with `E_STATE`, quoting the reason, unless
`override_blocked=true`. The override takes the task as before and
removes the reason. Re-adoption of a live claim by the same worker is
unchanged. `show_item` returns `blocked_reason` while the task is blocked.

### 2. Decisions and open questions

A new append-only detail key `decisions` holds rows
`{id, kind, text, source?, answers?, recorded_at, actor, session}`.
A `decision` must name its `source` (date, channel, message), so a later
session cannot present an invented decision as the owner's. It may
`answers` one earlier `question` on the same task; each question is
answered at most once. Rows are never edited or removed. `actor` and
`session` come from the caller exactly as session history records them,
so a caller without a session is refused, as with `takeover`.

The new tool `record_decision(id, text, kind, source, answers)` needs no
claim, like `defer`, and is refused on closed tasks. It records a `decide`
session-history entry; readers already accept unknown operation names.
A task with an unanswered question is not ready, and `finish` refuses it
with the new code `E_OPEN_QUESTIONS`, mirroring `E_OPEN_DEFERRALS`.
`show_item` returns `open_questions` and the `decisions` log, inline when
it fits the section budget and otherwise paged by row through
`show_item field="decisions"`, the same way as `sessions`.

### 3. Acceptance progress

A new detail key `acceptance_met` lists the exact texts of met criteria.
`update_item` takes `acceptance_met` as zero-based indexes into
`acceptance` as it stands after the same call; an empty list clears it,
and an out-of-range or duplicate index is `E_STATE`. Texts rather than
indexes keep the record meaningful if an older client reorders
`acceptance`. When this client rewrites `acceptance`, texts that no
longer match are dropped. `show_item` returns `acceptance_met` as indexes
into the current list plus `next_step`, the first unmet criterion.

### 4. Size limits

Writable content is the UTF-8 length of the compact JSON of the title and
the detail keys `description`, `context`, `acceptance`, `acceptance_met`,
`links`, `decisions` and `blocked_reason`; the cap is 8192 bytes. The
whole detail file is capped at 16384 bytes, measured without the
archival `legacy` key and the system-written keys `sessions`, `batch`,
`prepared` and `final_evidence`; what remains is agent-written content,
including deferrals. `create_item`, `update_item` and `record_decision`
refuse a write that exceeds either cap **and** grows writable content.
Session history and other system data therefore never cause a refusal,
however long they grow, and an item that is already oversized stays
readable and editable as long as an edit does not grow it. Bounding
session history itself belongs with the separate cleanup of `legacy`.

The refusal uses a new code, `E_ITEM_TOO_LARGE`. `E_OVERSIZED` already
means that a *response* exceeds the output cap and the caller should read
one section at a time. Reusing it for an oversized *input* would give one
code two different recoveries. The message names the cap and the size and
says to move long material to a linked document.

### 5. Credential check on writes

Before an operation stores agent-supplied text, it scans only the newly
supplied values: title, description, context, acceptance entries, links,
decision text and source, blocked reason, deferral summary and reason,
dismissal reason and takeover reason. Stored content is never scanned, so
an older item cannot become unwritable, and promoting a deferral does not
rescan its stored reason. The patterns live in one table,
`store.SECRET_PATTERNS`: AWS access key IDs and presigned URL parameters,
GitHub tokens, private key blocks, Slack, Anthropic, OpenAI and Google API
keys, JWTs, and credentials in a URL. A match is refused with the new code
`E_SECRET`; the message names the field and the kind of credential and
never echoes the value. The patterns are deliberately narrow: they catch
common token shapes, not every secret, and the agent guidance still says
never to store credentials, raw logs or host configuration.

## Compatibility

Every new field is a detail-owned key in `items/<id>.json`, following
ADR 0009 and ADR 0010. The index shape and `SCHEMA_VERSION` are
unchanged. Older clients type-check only the detail keys they know, so
they load, ignore and preserve the new keys.

Newer clients tolerate what older clients can legitimately write:

- A `blocked` task with no reason is valid; `take` still needs the
  override.
- A `blocked_reason` left on a task an older client unblocked is valid.
  Readers ignore it, and the next write from a newer client removes it.
- `acceptance_met` texts that no longer match `acceptance` after an older
  client rewrote it are valid. Readers ignore them.

An older client does not enforce the new rules: it takes blocked tasks,
finishes tasks with open questions, and writes past the caps without a
credential check. Upgrade every client with access to the state branch
before relying on them.

## Tool surface (ADR 0006 G9)

G9 keeps the surface small. `record_decision` is added as a separate
mutation tool for the same reason ADR 0010 added the deferral verbs:
`update_item` has replace semantics, and an append-only log whose rows no
operation may edit cannot be expressed through it without letting any
caller rewrite or drop the owner's decisions. `reason`, `acceptance_met`
and `override_blocked` are parameters on existing tools. The MCP surface
grows from 19 to 20 tools, and the measured startup schema-plus-guidance
cost from about 4.4k to 4.9k tokens.

## Rejected alternatives

- **A free-text `next_action` field.** It duplicates `needs`, readiness
  and `next_step`, and drifts from the state it describes.
- **An append-only progress log.** It recreates the handoff file inside
  the tracker; `sessions` already records history.
- **Changing `acceptance` entries to objects with a met flag.** Older
  clients type-check `acceptance` as a list of strings and would fail to
  load the tracker.
- **A schema version bump.** It would strand every older client to gain
  fields they can safely ignore.

## Out of scope

Removing the `legacy` key from detail files and archiving terminal items
out of the index are separate changes.
