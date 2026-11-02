# ADR 0012: Batch v2 — the integration-branch delivery loop

- **Status**: Proposed
- **Date**: 2026-11-01
- **Relationship to prior ADRs**: amends ADR 0007 (prepared-batch amendment:
  schema 3, `prepared_work_v1`), ADR 0008 (batch ownership no longer follows a
  takeover), and ADR 0006 G2 (one read-only verb joins the floor CLI). ADR 0003
  §2.1–2.3 and §2.7 (advisory identities, one live claim, one server = one
  worker) are unchanged.

---

## 1. Context and Problem Statement

A downstream consumer delivers a batch of tasks through one integration branch
and one final PR. A leader registers the batch, owns the integration worktree,
lands each member, records it, and ships the final PR. Review fixes after the PR
opens are leader commits. Members are implemented by other sessions that merge
the current integration head into their branch before they stop.

todo-db 0.12.0 cannot run this loop. Six blockers were found on 2026-11-01 by
code reads and throwaway fixtures against the 0.12.0 wheel:

| # | 0.12.0 behaviour | Where |
|---|---|---|
| B1 | Any worker can `take` an unclaimed registered member, so the leader's own `take` later fails with "claimed by". | `op_take` |
| B2 | A member without implementation dependencies must start exactly at the batch start head, a dependent one exactly at the current integration head; a member that merged in the current head cannot be prepared. | `op_prepare`, `TrackerService.prepare` |
| B3 | The integration head advances only inside `prepare`; a leader fix commit or a develop merge cannot be recorded. | `op_prepare` |
| B4 | `bind_batch_pr` binds once and requires the head to equal the registered head, so the first review fix strands the final PR. | `op_bind_batch_pr` |
| B5 | Any `update_item` change, including `context_append`, deletes the member's receipt and its dependents' receipts. | `op_update` |
| B6 | Accepted files must stay byte-identical (`_unchanged_paths`), which rejects legitimate same-file evolution; and a dead leader cannot be recovered without a live member claim (ADR 0008 only transfers ownership as a side effect of taking over a live member claim, and the transfer then invalidates every receipt because receipts bind `owner_generation`). | `_unchanged_paths`, `op_takeover`, `validate_snapshot` |

This ADR specifies the replacement semantics. It is design only; the
implementation lands in later changes that cite the section numbers below.

Vocabulary used throughout:

- **owner**: the worker recorded on the batch (`owner`), holding the **owner
  generation** (`owner_generation`, a 32-hex credential chosen at registration or
  issued by recovery). Owner-only operations need both.
- **recorded head**: the batch `start_head` or any head in the batch's
  `integration_log` (§2.3).
- **receipt**: the member's prepared evidence, `detail["prepared"]`.
- **current-acceptance evidence**: a passed rerun of a member's own
  verification at the final integration head (§2.5).

## 2. Decision

### 2.0 Cross-cutting rules

- **Gate codes.** Two codes are added to `errors.py` (additive; existing codes and
  exit codes are unchanged):
  - `E_BATCH_OWNER`: the caller is not the batch owner for an owner-only action.
    The message names the owner; recovery steps say whom to ask and how to
    recover (§2.1). Kind is `gate`.
  - `E_BATCH_HEAD`: a head is not a recorded head, does not descend from the
    required head, or unrecorded commits sit between the registered head and the
    submitted head. Kind is `gate`.

  Everything else reuses existing codes: `E_CLAIM_STALE` (wrong owner worker or
  owner generation, as in 0.12.0 `abort_batch`/`bind_batch_pr`), `E_CONFLICT`
  (compare-and-swap mismatch, different PR), `E_FINAL_EVIDENCE`, `E_STATE`,
  `E_SCHEMA`, `E_ACTIVE_CLAIMS`.
- **Owner generation is a credential and is never served by any read** (same rule
  as claim generations, `_public_session_entry`). Readers see `owner` and
  `owner_epoch` (§2.1) only. v2 receipts therefore store `owner_epoch`, not the
  generation.
- **Every new refusal fails closed** with a stable code and at least one recovery
  step, and changes no state. `kind` follows `err()`: the `E_STATE` refusals
  below are tagged `error` (agents stop and report), the others `gate`. Refusals
  an agent can act on use a gate code: a missing rebind guard is `E_CONFLICT` and
  a fix outside scope is `E_BATCH_HEAD` (not `E_STATE`). The 0.12.0 refusal for a
  missing cutover flag stays `E_STATE` (existing contract).
- **Path sets are rename-free.** Every path set in this ADR (member
  `changed_files`, advance `changed_files`, the unrecorded-commit diff, overlap
  inputs, final `changed_files`, the excluded set) is
  `git diff --no-renames --name-only -z <a>..<b>`, so a rename lists both the
  deleted and the added path. Without this a member renaming an earlier member's
  file would hide the deleted path from overlap and scope checks.
- **Idempotent results write nothing.** An idempotent result is decided inside
  `apply`; the service returns it as `{ok:true, data:{…, idempotent:true}}`
  without committing. `_mutate` gains a no-write success path for results marked
  `idempotent: true`, and only those (the implementation carries the marker as
  an internal `_no_write` ack key that never reaches the caller) (today a no-change tree is refused
  `E_STATE` "operation produced no state change"; `abort_batch`'s idempotent path
  keeps its legacy behaviour).
- **Audit surfaces.** Every new mutation has a commit trailer/summary naming actor
  and operation and, where it changes a batch, an entry on the batch record
  (§2.1, §2.3, §2.6). `recover` additionally writes member session history.
  `SESSION_OPS` gains `recover`; `advance` and rebind are batch-record and
  trailer-only like `register`/`bind` today.
- **Batch lifecycle gate.** Every batch mutation below requires
  `lifecycle == "active"` (`E_STATE` otherwise), as 0.12.0 does.
- **No hidden clock or heartbeat** (ADR 0003 §2.8): nothing here expires.

### 2.1 Owner-only claims on active-batch members, and owner recovery

**Rule.** While a batch is `active`, only its owner may `take`, `takeover`,
`prepare` or `finish` a member of it. Membership is `item_id in batch.members`
for any batch with `lifecycle == "active"`, regardless of whether the item
carries `batch` metadata (registration does not require it).

- `take` or `takeover` of a member of an active batch by any other worker is
  refused with `E_BATCH_OWNER`: `task 'm1' belongs to active batch 'b1' owned by
  'leader'; only the owner may claim it`. Recovery steps: ask the owner; or, if
  the owner is dead, `recover_batch_owner` (below). The check runs before the
  claim check, so a foreign worker learns the owner, not just "claimed by".
- `prepare` and `finish` re-check that the claim holder is the current owner. A
  previous owner that kept a live member claim after being recovered can
  `release`/`renew` it (its own claim) but cannot prepare or finish: `E_BATCH_OWNER`.
- The owner's `take` succeeds as today. `takeover` by the owner of a live member
  claim held by someone else (a claim made before registration) is the ADR 0008
  operation unchanged, except that it no longer transfers batch ownership (the
  `transferred_batches` ack key is removed: with owner-only claims the owner is
  the taker, and a dead owner is handled by recovery).
- Members of `aborted` or `completed` batches follow ordinary claim rules.
- Reserved members still appear in `list_items ready_only` for non-owners (the
  queue is not caller-filtered) and are refused on `take`, as designed.
- `register_batch` refuses with `E_CONFLICT` if any member holds a live claim of a
  worker other than the registering owner, naming the members and holders. This
  removes the orphaned-claim state the 0.12.0 fixture produced.
- Non-claim edits (`update_item`, `drop`, `defer`) are not owner-gated: they stay
  advisory-audited under ADR 0003 §2.7. Only claims are reserved.

*Failure mode prevented:* B1 — an unrelated session (or a member worker) takes a
member so the leader's `take`/`prepare` cannot proceed.

**`recover_batch_owner(batch_id, expected_owner, expected_owner_generation,
new_owner_generation, reason)`.** For a dead leader. The caller generates
`new_owner_generation` (32 hex) and writes it to its own ledger **before** the
call; the server stores it and never returns it. A lost reply therefore cannot
lose the generation (a retry is a new call with a new operation ID, so a
server-issued value could not be re-served). Semantics:

1. Requires a session-attributed caller (`E_STATE` otherwise, as `takeover`).
2. The batch must be `active` (`E_STATE`). Member claims are **not** required and
   **not** touched, so recovery works with no live member claim (B6).
3. **Idempotency, checked first:** if `batch.owner == caller`,
   `batch.owner_generation == new_owner_generation`, and the last `recoveries`
   entry has `previous_owner == expected_owner`, return
   `{idempotent: true}` with no write (§2.0).
4. `expected_owner` must equal the recorded owner (`E_CONFLICT`: "owner changed;
   re-read with show_batch"). `expected_owner_generation` must equal the recorded
   owner generation (`E_CLAIM_STALE`; the message never reveals the real value).
   `new_owner_generation` must differ from the current one (`E_STATE`). `reason`
   is validated like a takeover reason (one bounded line).
5. The caller becomes the owner (the caller may be the current owner itself, which
   rotates the generation after the leader lost its ledger). The previous
   generation becomes stale: every owner-only call carrying it fails
   `E_CLAIM_STALE`. `owner_epoch` increments by one; `register_batch` sets it to
   0, and `batch_schema: 1` records carry no epoch and are never compared.
6. Audit: the batch record gains an entry in `recoveries`
   (`{epoch, previous_owner, new_owner, reason, session_id, client, op_id, at}`;
   no generations); every member item gets a `recover` session entry
   (`reason`, `displaced = previous_owner`) under the same operation ID; the
   commit summary reads `<caller> recovers batch <id> from <owner>: <reason>`.

Where the recovering session gets `expected_owner_generation`: from the leader's
own ledger (the consumer stores the generation it registered with next to the
integration worktree), or from the state branch files by a human with Git
access. No API serves it. The generation guards against stale or confused
callers, not against an agent that reads the state branch (`index.json` stores
it); recovery stays advisory under ADR 0003 §2.7 and is an assertion of death,
not a proof (ADR 0008 §2).

Receipts stay valid across recovery: receipt validity requires
`receipt.owner_epoch <= batch.owner_epoch`, not equality (0.12.0 required
generation equality, which made every transfer fail validation).

*Failure mode prevented:* B6 (dead leader strands the batch: abort and bind need
the original owner) and the 0.12.0 transfer that broke every receipt.

### 2.2 Batch read: `show_batch` (MCP) and `todo-db batch show` (CLI)

One read operation, two surfaces over the same service method
`TrackerService.show_batch`, so the data is identical.

- MCP tool `show_batch(batch_id, field=None, offset=0, budget=6000, rev=None)`.
- Floor CLI `todo-db batch show BATCH_ID [--field F] [--offset N] [--budget N]
  [--rev R]` with the standard target options. BenchBox-style scripts cannot
  call MCP tools; ADR 0006 G2 is amended: this read-only verb joins the floor CLI
  because CI and landing scripts consume it. It reads through the same cached,
  never-writing path as `todo-db show`; a read failure is exit 2 with the same
  `{ok:false, code, error, recovery, kind}` envelope, success is exit 0 with
  `{ok:true, data}`. A read-only CLI test asserts the remote state branch tip is
  unchanged and no commit is created; the cache may fetch, as `todo-db show` does.

**Default response** (always within `MAX_BYTES` = 16 KiB, measured as the existing
`_wire` serialization): `batch_id`, `lifecycle`, `project_id`, `repository`,
`integration_branch`, `integration_worktree`, `start_head`, `integration_head`,
`scope_hash`, `delivery_boundary`, `terminal_outcome`, `owner`, `owner_epoch`,
`final_pr` (`{repository, number, node_id, head}` or null), `store`
(`{schema_version}`), `capability` (§2.8), `rev` (or `cached_rev` + `stale`), and
`sections`, a map from section name to `{bytes, items}` for the paged fields:

| Section | Content |
|---|---|
| `members` | per member: `id`, `status`, `claim_worker` or null, `prepared` (bool), `receipt_schema`, `accepted_head`, `source_base`, `integration_head`, `integration_seq`, `changed_files_count` |
| `scope` | per-member frozen scope patterns |
| `receipts` | per member: the full receipt (`changed_files`, `overlaps`, `verification`, …) |
| `integration_log` | §2.3 entries |
| `acceptance_required` | derived, per member: reasons (§2.5) |
| `recoveries` | §2.1 entries |
| `final_pr_history` | §2.6 entries |
| `final_evidence` | per finished member |

A section is inlined when it fits in the remaining budget, in table order;
otherwise the response carries a `continuation`
(`{field, offset, budget, rev}`) exactly as `show_item` sections do, and
`show_batch field=receipts offset=… budget=…` returns a bounded window of that
section's canonical JSON. `rev` pins a snapshot; a moved revision is
`E_CURSOR_STALE`. A single section window above `MAX_BYTES` is `E_OVERSIZED`.
`acceptance_required` is derived on read, never stored. Unknown batch is
`E_STATE`. `owner_generation` is never served.

*Failure mode prevented:* consumer scripts cannot read receipts, advances or the
binding without MCP, so they cannot check batch readiness.

### 2.3 Dispatch bases and the integration log

**State.** The batch record gains `integration_log`, an append-only list; entry
`seq` 0 is the start head:

```
{seq, head, previous_head, kind: "start"|"prepare"|"reprepare"|"fix"|"develop-merge",
 member?, changed_files?, requires_acceptance?, merged_parent?, op_id, at}
```

`integration_head` is the head of the last entry; the **recorded heads** are
every `head` in the log. Both `prepare` (kind `prepare`) and
`advance_batch_head` (§2.4) append. Nothing rewrites an entry.

**Rule.** `prepare` accepts `source_base` equal to a recorded head (the start
head or any logged head) and refuses any other with `E_BATCH_HEAD`: "source base
is neither the batch start head nor a recorded integration head". This replaces
the 0.12.0 split (root members at the start head, dependent members at the
current head), including the service-layer re-check at the same lines.

**Changed files** are measured `git diff --no-renames --name-only -z source_base..accepted_head`
in the member's own checkout. A member that merged the current integration head
into its branch and was dispatched from that head lists only its own files. The
per-member frozen scope check is unchanged: any changed path outside the member's
frozen scope patterns is refused (`E_STATE`, "changes a path outside its frozen
scope").

**Other `prepare` checks** (the pure-store ones repeat on every publication
retry; the Git ones run in the service):

- `source_base` is an ancestor of `accepted_head` (kept).
- Each implementation dependency's `accepted_head` is an ancestor of this
  member's `accepted_head` (kept). The dependent no longer needs a base equal to
  the current head; any recorded head that contains the dependency works.
- `integration_head` (the head the leader produced by landing this member) must
  strictly descend from the registered integration head and contain
  `accepted_head` and every earlier accepted member (`E_BATCH_HEAD`), except in
  re-prepare (§2.3a).
- **No unrecorded commits.** Every path in
  `git diff --no-renames --name-only <registered integration head>..integration_head` must be in
  this member's `changed_files`. A leader fix commit made before landing must
  first be recorded with `advance_batch_head`; otherwise it would escape overlap
  and acceptance accounting (`E_BATCH_HEAD`, recovery: advance first). The check
  compares paths only: a pre-landing commit limited to the landing member's own
  paths is indistinguishable from merge resolution and is attributed to that
  member.
- The caller is the owner and holds the live claim (`E_BATCH_OWNER`,
  `E_CLAIM_STALE`); the integration worktree is clean at `integration_head` on the
  registered branch and repository (kept).

**Receipts.** `prepared_work_v2` (§2.8) keeps the integration head the member was
integrated at: `receipt.integration_head` and
`batch.integrated_members[member] = {accepted_head, integration_head,
integration_seq}` are written at prepare and are never rewritten by another
member's prepare; only re-prepare of the same member replaces its own entry
(§2.3a). 0.12.0 rewrote every
earlier member's `integrated_members.integration_head` on each later prepare;
that loop is removed. Readiness of a dependent compares the dependency's
`accepted_head` with the current integration head by ancestry only.

### 2.3a Re-prepare after invalidation

Any `prepare` of a member that has no receipt but an existing `integrated_members`
entry replaces that member's own `accepted_members`/`integrated_members` entries:
log `kind: "reprepare"` when `integration_head` equals the registered head (no
new commit), `kind: "prepare"` (normal strict descent) otherwise. The text below
specifies the no-new-commit case.

An invalidating edit (§2.7) deletes a receipt whose content is already in the
integration head. A member with no receipt but an existing `integrated_members`
entry may be prepared again with `integration_head == registered integration_head`
when `accepted_head` is already an ancestor of it (no new commit needed; the
strict-descent rule does not apply). The new receipt replaces that member's
`accepted_members` and `integrated_members` entries and appends an
`integration_log` entry with `kind: "reprepare"` and no head change. Overlaps
are recomputed from the member's `changed_files`. Refusal codes are those of
`prepare`. The `reprepare` entry's `head` equals the previous entry's `head`, so
the recorded-head set is unchanged. Any member needing re-prepare after the head
has moved is prepared at the current head by the same rule (the accepted head
remains an ancestor).

*Failure mode prevented:* B2 (and a batch stranded after a legitimate invalidating
edit).

### 2.4 Owner-only integration-head advance

`advance_batch_head(batch_id, owner_generation, expected_head, head, reason,
changed_files)`.

| Argument | Meaning |
|---|---|
| `expected_head` | the integration head the owner believes is current (full SHA); compare-and-swap |
| `head` | the new head (full SHA) the owner committed in the integration worktree |
| `reason` | `fix` or `develop-merge` |
| `changed_files` | sorted, de-duplicated paths changed by the advance |

Checks, in order (all state-free on failure):

1. Owner worker and generation: `E_CLAIM_STALE`. Active batch: `E_STATE`.
2. No member is `done`: `E_STATE` ("closeout has started; resume closeout instead
   of changing the head").
3. `expected_head` equals the registered integration head: `E_CONFLICT` ("stale
   expected head; re-read with show_batch"). Exception for idempotency below.
4. Descent: `expected_head` is a strict ancestor of `head` in the integration
   worktree: `E_BATCH_HEAD`. The worktree must be clean at `head` on the
   registered branch/repository (`E_STATE`, as `register_batch`).
5. `changed_files` equals `git diff --no-renames --name-only -z expected_head..head` exactly
   (`E_STATE`; the service recomputes, the caller's list is a claim).
6. `reason == "fix"`: every changed file matches the batch's union of member
   scopes, otherwise `E_BATCH_HEAD` (a fix cannot widen scope).
7. `reason == "develop-merge"`: `head` has exactly two parents and the first is
   `expected_head` (a real `--no-ff` merge; a fast-forward is refused as
   `E_BATCH_HEAD`), and the second parent is recorded as `merged_parent`. Two
   further checks keep a mislabelled merge from hiding member changes: (a)
   `merged_parent` must be an ancestor of the batch's registered `develop_ref`
   (§2.8) in the integration worktree; a batch registered without one refuses
   every `develop-merge` (`E_BATCH_HEAD`); (b) every path in `changed_files` must also differ
   between `merge-base(expected_head, merged_parent)` and `merged_parent`,
   otherwise `E_BATCH_HEAD` ("merge introduces non-develop content"). The paths
   are then exempt from the scope check, because they come from the integration
   base, not from members. `develop_ref` is fixed once at `register_batch` (an immutable batch field under
   `refs/remotes/`, for example `refs/remotes/origin/develop`) because the
   registered `delivery_boundary` is a free-form token (`final-pr` in practice),
   not a branch name; the owner cannot pick a convenient ref per call.

**Effects.** One `integration_log` entry is appended with `kind` = the reason,
`previous_head`, `head`, `changed_files`, `op_id`. The registered
`integration_head` becomes `head`, and `head` is a valid dispatch base from then
on (§2.3). For `fix`, the entry's `requires_acceptance` lists every member whose
receipt `changed_files` intersects the advance's files at the time of the advance
(fix touching earlier member files, §2.5). For `develop-merge`,
`requires_acceptance` is empty and the files join the **excluded set** (§2.5).

**Idempotent retry.** If the log contains an entry whose `head` equals `head`,
with `previous_head == expected_head`, `reason` and `changed_files` equal to the
request (not necessarily the last entry: a later re-prepare may follow), the call returns the same result with `idempotent: true` and
writes nothing. Any other mismatch is the stale `E_CONFLICT`.

*Failure mode prevented:* B3; and unaccounted fix content that would bypass
overlap/acceptance accounting.

### 2.5 Recorded overlap and current-acceptance evidence

A v2 client refuses `finish` of a member of a still-active legacy (`batch_schema: 1`)
batch with `E_SCHEMA`: byte-equality evidence is gone, and the cutover from schema 3
already refuses to run while such a batch is active.

`_unchanged_paths` byte-equality checks (at `prepare` in the service, in
`_batch_runtime_ready`, and in `_validate_final_checkout`) are removed. Same-file
evolution is legitimate; what must not happen is a later change silently undoing
an earlier member's accepted behaviour. The replacement records the overlap and
forces the earlier member's acceptance to be re-proven on the final tree.

**Recorded at `prepare`.** The receipt carries `overlaps`, a list of
```
{"kind": "member", "member": A, "paths": [...]}     # B's changed_files ∩ A's changed_files, A prepared earlier
{"kind": "fix", "seq": n, "paths": [...]}           # B's changed_files ∩ files of a fix log entry n with seq > seq(source_base)
```
Empty `paths` entries are not recorded. Members are compared by the exact paths in
their receipts. The second kind covers a member dispatched from
before a fix that lands after it. It proves B's own behaviour on the final tree;
fix content that no member owns is covered only by the consumer's final-tree
suite.

**Acceptance requirement (derived, §2.2).** The set `R` of members that need
current-acceptance evidence at final is:
- every `A` named by a `kind: member` entry in any later member's receipt;
- every `A` in some `fix` log entry's `requires_acceptance`;
- every member `B` with a `kind: fix` overlap in its own receipt.

A `develop-merge` entry never adds to `R`: its files are integration base content,
verified by the final-tree suite the consumer already runs on the PR.

**Final evidence.** `finish` for a prepared member requires, in addition to the
0.12.0 evidence, `current_acceptance`: a map `member_id → {status: "passed",
revision, clean: true, suite, command, worktree}` that contains every member of
`R` (extra members allowed, each validated), where for each: `revision` equals the
final `integration_head` (and so the final PR head); `worktree` equals the
registered integration worktree; `clean` is true; `suite` and `command` equal that
member's receipt `verification.suite`/`verification.command` (the member's own
acceptance, not a weaker one). Missing or mismatched entries are refused with
`E_FINAL_EVIDENCE`, naming the member and the reason that put it in `R`.

**Other final checks:**
- The 0.12.0 checks remain: batch/branch/scope hash/repository, `integration_head`
  equals the registered head, `final_pr` equals the binding exactly, per-member
  heads and ranges bind the receipts (`range.base == receipt.source_base`).
- Ancestry replaces byte-equality: the final tree contains `start_head`, every
  logged head and every member's `accepted_head` (`E_FINAL_EVIDENCE`).
- `changed_files` must equal `git diff --no-renames --name-only start_head..tree_revision`
  **minus the excluded set**. The excluded set is the union of `develop-merge`
  entries' files, minus every path in a member receipt's `changed_files` or in a
  `fix` entry's files (a path that a member or fix also touched stays subject to
  scope). Each remaining path must match the union of member scopes. A
  `develop-merge` therefore never needs to fit a member scope, and never hides a
  member or fix change to the same path.

**Silent-revert counterexample.** B descends from A (so B's base is a recorded
head containing A's work), restores A's line in a shared file and lands last. B's
`prepare` records `{kind: member, member: A, paths: [f]}`; A is in `R`; final
evidence without A's acceptance rerun at the final head is refused
(`E_FINAL_EVIDENCE`); with the rerun, A's own suite fails on the reverted line, so
the evidence cannot pass. The rerun is caller-attested under ADR 0003 §2.4: the
tracker checks shape and binding, not execution.

*Failure mode prevented:* B6 first half (byte equality rejects legitimate
evolution) without opening the silent-revert hole byte equality was closing.

### 2.6 Revisable final-PR binding

`bind_batch_pr(batch_id, owner_generation, pr_repository, number, node_id, head,
expected_previous_head=None)`. The binding is
`final_pr = {repository, number, node_id, head}`; `repository` is the PR's
repository slug (the batch `repository` may be a path or URL, so the PR's own is
stored) and evidence must carry the same object.

| State | Request | Result |
|---|---|---|
| no binding | `head == integration_head`, no `expected_previous_head` | bind; log entry |
| no binding | `head != integration_head` | `E_CONFLICT` |
| no binding | `expected_previous_head` given | `E_CONFLICT` (nothing to revise) |
| bound | identity differs in `repository`, `number` or `node_id` | `E_CONFLICT` ("different PR; one batch has one final PR") |
| bound, same identity | `head == bound head` and `expected_previous_head` absent or equal to the last history entry's `previous_head` | idempotent success, no write |
| bound, same identity | `head == bound head`, `expected_previous_head` given and different from the last history entry's `previous_head` | `E_CONFLICT` (stale) |
| bound, same identity | `head != bound head`, `expected_previous_head` absent | `E_CONFLICT` (guard required) |
| bound, same identity | `head != bound head`, `expected_previous_head != bound head` | `E_CONFLICT` (stale) |
| bound, same identity | `head != bound head`, expected matches, `head != integration_head` | `E_CONFLICT` |
| bound, same identity | `head != bound head`, expected matches, `head == integration_head` | rebind: history entry, head replaced |

Other rules: stale owner worker/generation is `E_CLAIM_STALE`; a rebind that
changes the head is refused `E_STATE` once any member is `done`; an idempotent
result is returned per §2.0. A head change
**invalidates final evidence**: evidence validates only if its
`integration_head` and `final_pr` equal the registered head and binding, so any
evidence built for the previous head is refused by head equality (`E_FINAL_EVIDENCE`),
and a rebind clears any `final_evidence` still stored (none can exist before a
member is done; the clear keeps the invariant structural). `final_pr_history`
appends `{previous_head, head, op_id, at}` on every change. The normal review-fix
sequence is `advance_batch_head(reason=fix)`, push, `bind_batch_pr(head=new,
expected_previous_head=old)`.

*Failure mode prevented:* B4 — the first review fix strands the final PR; and a
second PR silently replacing the first.

### 2.7 Metadata-safe edits

`op_update` keeps a prepared member's receipt (and its dependents' receipts) for
edits whose changed set is **only** `context` (including `context_append`). That
is the sole exemption. Every other edit still deletes the receipt and any
`final_evidence`, then invalidates dependents transitively through
`_invalidate_prepared_dependents`: `description`, `acceptance`, `links`, `needs`,
`title`, `priority`, `status` and `not_before`. The receipt binds the exact member
content and readiness edge, and only `context` is a free-form progress channel.

`scope` and `batch` are not mutable item fields on a registered member: the
frozen scope lives in the immutable batch registry and has no edit path, and
registered `batch` metadata is refused (`E_STATE`, "registered batch metadata is
immutable; abort the batch before reassignment") when the new value differs. Both
therefore cannot leave a receipt over changed content, and the refusal leaves
receipt and dependents untouched. An edit mixing `context` with any other field
invalidates. Completed prepared members still refuse edits (`E_STATE`, kept). While any member
of a batch is `done`, every receipt-invalidating edit on any member of that batch
is refused `E_STATE` ("closeout has started"), because the head can no longer
move and a receipt could not be rebuilt; `context`-only edits are still allowed.
After an invalidating edit before closeout, the member is prepared again by
re-prepare (§2.3a).

**Deviation from todo-db-batch-head-advance-rebind-20261101 acceptance #4**: scope
and registered-batch edits are refused, not invalidating. That item's acceptance
is amended to match before implementation starts, and the amendment is recorded in
the item before the contract item is closed.

*Failure mode prevented:* B5 — a progress note destroys prepared work and its
dependents, while no content-affecting edit can outlive a stale receipt.

### 2.8 Versioned capability, schema, and one-step migration

**Names.**
- Receipt schema: `prepared_work_v2` (`PREPARED_RECEIPT_SCHEMA`). Receipt keys:
  the v1 keys minus `owner_generation`, plus `owner_epoch` (int), `integration_seq`
  (int), `overlaps` (list, §2.5). `integration_head` keeps v1 meaning.
- Batch record: `develop_ref` (optional, set once at registration, never changed),
  `batch_schema: 2`, `owner_epoch`, `integration_log`, `recoveries`,
  `final_pr_history`; `final_pr` gains `repository`.
- Store schema: **`schema_version` 4** (`SCHEMA_VERSION = 4`;
  `SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3, 4)`).
- Capability: **`batch-v2`**, version 2. The release shipping it is **todo-db
  0.13.0** (`tool_version` is `0.13.0`).

**Advertisement.** The server's `initialize` result carries
`capabilities.experimental["todo_db_batch"]`:
```
{"version": 2, "receipt_schema": "prepared_work_v2",
 "store_schema_version": 4, "supported_schema_versions": [1,2,3,4],
 "tool_version": "<package version>",
 "tools": ["advance_batch_head","recover_batch_owner","show_batch"]}
```
implemented by wrapping the low-level server's `create_initialization_options` to
add the `experimental` key; `serverInfo.version` stays the package version. The
same object is `todo_db.BATCH_CAPABILITY` and the `capability` field of
`show_batch`/`todo-db batch show`. A consumer refuses a runtime without
`todo_db_batch.version >= 2` (an 0.12.0 server has no such key). It must key on
the capability and receipt schema, not on the package version string.

**Store cutover.** One cutover, directly from the current schema to 4, performed
by the first `register_batch` behind the existing flag. The flag keeps its name
`confirm_schema3_cutover` (changing it would break every caller for no benefit);
its documented meaning is "confirm the cutover to the current batch schema". It is
required whenever `schema_version < 4`; without it the call is refused `E_STATE`
and nothing changes.

- From schema 1 or 2 (no batches, no receipts): sets `schema_version = 4` and
  writes the new batch record in the same commit. No other file changes.
- From schema 3: refused with `E_STATE` if any batch is `active` (finish or abort
  it under the writing client first); otherwise terminal batch records and their
  v1 receipts/evidence are kept unchanged as history and marked
  `batch_schema: 1`, and the new record is written in the same commit.
- Both: refused with `E_ACTIVE_CLAIMS` if any live claim is held by a worker other
  than the caller, naming holders (stricter than 0.12.0; the pilot precondition
  is "no live claim held by another worker"). The flag asserts every process with
  access to the state branch is stopped or upgraded (ADR 0007 amendment, kept).

**Only the cutover moves the schema.** Only `register_batch` with the flag changes
`schema_version`. `op_create` and `op_update` never raise it (today they raise it
to `SCHEMA_VERSION` when writing batch metadata, which would silently cut over
and lock out 0.12.0 clients mid-batch); below schema 4, writing item `batch`
metadata is refused `E_SCHEMA` with a recovery step pointing at the cutover.

**Compatibility.**
- A 0.12.0 client reads `schema_version` 4, finds it outside
  `SUPPORTED_SCHEMA_VERSIONS` and refuses with `E_SCHEMA` before reading any item
  (`validate_snapshot`). It cannot bypass the v2 rules. This is the point of the
  cutover, and Gate 1 proves it.
- A v2 client keeps reading and writing ordinary schema 1–3 stores. Batch
  mutations (`prepare`, `advance_batch_head`, `recover_batch_owner`,
  `bind_batch_pr`) on a store below schema 4 are refused `E_SCHEMA` pointing at
  the cutover. `prepared_work_v1` receipts are valid only on `batch_schema: 1`
  history; under a `batch_schema: 2` batch a v1 receipt is rejected `E_SCHEMA`.
  v1 receipts are never reinterpreted under v2 rules (no overlaps, no epoch).
- Downgrade is not supported: a schema-4 store is not rewritten back.

*Failure mode prevented:* a mixed-version fleet bypassing owner reservation, base,
overlap and acceptance rules by writing through an older client.

## 3. The six 0.12.0 blockers: new behaviour and proving test

All tests live in `tests/test_batch_v2.py` (store-level and service-level, real
temporary Git repositories) unless noted. Each asserts behaviour.

| # | New behaviour | Proving test |
|---|---|---|
| B1 | `take`/`takeover` of an active-batch member by a non-owner is refused `E_BATCH_OWNER` naming the owner; the owner's `take` still succeeds. | `test_foreign_take_of_active_member_refused_with_owner_code` (must fail against 0.12.0); `test_owner_take_succeeds_after_foreign_refusal`; `test_members_of_aborted_batch_follow_ordinary_claims` |
| B2 | A no-dependency member started from any recorded head prepares; an unrecorded base is refused `E_BATCH_HEAD`; changed files are measured from that base and scope stays frozen. | `test_synced_member_prepares_from_recorded_integration_head`; `test_unrecorded_base_refused`; `test_registered_member_without_metadata_is_reserved`; `test_synced_member_receipt_lists_only_own_files`; `test_path_outside_member_scope_still_refused` |
| B3 | `advance_batch_head` records fix and develop-merge heads with reason and files; the new head is a dispatch base. | `test_fix_advance_recorded_and_becomes_dispatch_base`; `test_advance_stale_expected_head_refused`; `test_advance_non_descendant_refused`; `test_advance_non_owner_refused`; `test_advance_exact_retry_idempotent`; `test_unrecorded_commit_before_prepare_refused` |
| B4 | `bind_batch_pr` moves the bound head with `expected_previous_head`, same PR identity only, exact retry idempotent, evidence for the old head refused. | `test_rebind_moves_head_with_expected_previous_head`; `test_rebind_different_pr_refused`; `test_rebind_retry_idempotent`; `test_evidence_for_previous_pr_head_refused` |
| B5 | `context` and `context_append` keep receipt and dependents; each of `description`, `acceptance`, `links`, `needs` still invalidates transitively; `scope`/registered `batch` edits are refused leaving the receipt. | `test_context_edit_keeps_receipt`; `test_context_append_keeps_receipt_and_dependents`; one test per other field: `test_description_edit_invalidates_receipt_and_dependents`, `test_acceptance_edit_…`, `test_links_edit_…`, `test_needs_edit_…`, `test_batch_metadata_edit_refused_receipt_kept`, `test_scope_has_no_edit_path_receipt_kept`; `test_mixed_context_and_acceptance_edit_invalidates` |
| B6 | Same-file evolution is recorded as overlap and requires current-acceptance evidence at final; a dead leader is recovered with no live member claim. | `test_same_file_evolution_records_overlap`; `test_silent_revert_blocked_without_acceptance_rerun`; `test_fix_touching_earlier_member_requires_acceptance`; `test_develop_merge_excluded_from_union`; `test_recover_owner_with_no_member_claim`; `test_recover_wrong_owner_or_generation_refused`; `test_recover_retry_after_lost_reply_idempotent`; `test_recovered_previous_generation_is_stale`; `test_receipts_survive_recovery`; `test_rename_of_earlier_member_file_records_overlap`; `test_develop_merge_with_parent_outside_registered_develop_ref_refused`; `test_reprepare_after_invalidating_edit`; `test_reprepare_with_new_code_replaces_entries`; `test_invalidating_edit_refused_after_member_done` |

Remaining tests: `test_show_batch_pages_oversized_section`,
`test_show_batch_never_serves_owner_generation`,
`test_cli_batch_show_matches_tool_and_never_writes` (CLI);
`test_initialize_advertises_batch_v2_capability` (stdio);
`test_cutover_from_schema_1_one_commit_and_refused_without_flag`,
`test_cutover_from_schema_3_refused_with_active_batch`,
`test_batch_metadata_write_below_schema_4_refused`,
`test_cutover_refused_with_foreign_live_claim`,
`test_v1_client_refuses_schema_4_with_e_schema` (a fixture reader pinned to
`SUPPORTED_SCHEMA_VERSIONS = (1,2,3)`), `test_v1_receipt_not_reinterpreted_under_v2`.
Release Gate 1 additionally runs the real 0.12.0 wheel against the migrated
scratch store and expects `E_SCHEMA`.

## 4. API surface: new and changed

`id` args are item IDs; `batch_id` args are batch IDs. Revisions are full 40/64-hex
SHAs. Success results are the `data` of `{ok:true, data}`; refusals are
`{ok:false, code, error, recovery, kind}`.

### 4.1 MCP tools

| Tool | New/changed | Arguments | Success result | Refusal codes |
|---|---|---|---|---|
| `show_batch` | new | `batch_id`; `field=None`, `offset=0`, `budget=6000`, `rev=None` | summary + `sections` map + `capability` + `rev` (§2.2); with `field`: `{field, offset, next_offset?, total_bytes, text}` window | `E_STATE` unknown batch, `E_CURSOR_STALE`, `E_OVERSIZED`, `E_OFFLINE` |
| `recover_batch_owner` | new | `batch_id`, `expected_owner`, `expected_owner_generation`, `new_owner_generation` (caller-generated, 32 hex), `reason` | `{batch_id, owner, owner_epoch, recovered_members, idempotent}` | `E_CONFLICT` owner changed, `E_CLAIM_STALE` wrong generation, `E_STATE` not active / no session / bad reason, `E_SCHEMA` store below 4 |
| `advance_batch_head` | new | `batch_id`, `owner_generation`, `expected_head`, `head`, `reason` (`fix`\|`develop-merge`), `changed_files` | `{batch_id, integration_head, seq, reason, changed_files, requires_acceptance, idempotent}` | `E_CLAIM_STALE`, `E_CONFLICT` stale head, `E_BATCH_HEAD` (non-descendant, not a real merge, merge parent not under the registered `develop_ref`, no registered `develop_ref`, non-develop content, fix outside scope), `E_STATE` (files mismatch, member done, inactive), `E_SCHEMA` |
| `take` | changed | `id` | unchanged ack | adds `E_BATCH_OWNER` (active-batch member, caller not owner) |
| `takeover` | changed | `id`, `expected_holder`, `reason` | ack without `transferred_batches` | adds `E_BATCH_OWNER`; no ownership transfer |
| `prepare` | changed | unchanged arguments (`id`, `generation`, `batch_id`, `member_id`, `source_worktree`, `source_revision`, `source_base`, `accepted_head`, `integration_head`, `scope_hash`, `verification`, `implementation_dependencies`) | `{id, status:"open", prepared:{batch_id, member_id, source_worktree, source_revision, integration_head, integration_seq, overlaps}}` | adds `E_BATCH_OWNER`, `E_BATCH_HEAD` (base not recorded; head not descendant; unrecorded commits), `E_SCHEMA`; re-prepare (§2.3a) after an invalidating edit; keeps `E_STATE`, `E_CLAIM_STALE` |
| `bind_batch_pr` | changed | `batch_id`, `owner_generation`, `pr_repository`, `number`, `node_id`, `head`, `expected_previous_head=None` | `{batch_id, final_pr, previous_head?, idempotent}` | `E_CLAIM_STALE`, `E_CONFLICT` (different PR, guard missing, stale previous head, head ≠ integration head), `E_STATE` (member done, inactive), `E_SCHEMA` |
| `finish` | changed | `id`, `generation`, `final_evidence` adds `current_acceptance` map and `final_pr.repository` | unchanged | adds `E_BATCH_OWNER`; `E_FINAL_EVIDENCE` for each missing/mismatched acceptance entry |
| `update_item` | changed | unchanged | `{id, changed, invalidated}`; `context`-only edits report no `prepared_receipt` | unchanged codes; registered `batch` edits `E_STATE`; invalidating edits after any member is `done` `E_STATE`; batch metadata below schema 4 `E_SCHEMA` |
| `register_batch` | changed | existing arguments plus `develop_ref=None` (immutable; must start with `refs/remotes/`, required for any later `develop-merge`); `confirm_schema3_cutover` now confirms the schema-4 cutover | `{batch_id, members, start_head, schema_version, migrated_from}` | adds `E_ACTIVE_CLAIMS` (foreign live claim), `E_CONFLICT` (member claimed by another), `E_STATE` (no flag, active schema-3 batch); never silently migrated by `create_item`/`update_item` |
| `abort_batch` | unchanged | `batch_id`, `owner_generation` | unchanged | unchanged |
| `initialize` | changed | — | adds `capabilities.experimental.todo_db_batch` (§2.8) | — |

### 4.2 Floor CLI

| Command | New/changed | Arguments | Success | Refusals |
|---|---|---|---|---|
| `todo-db batch show` | new, read-only | `BATCH_ID`, `--field`, `--offset`, `--budget`, `--rev`, standard target options | same `data` as `show_batch`; exit 0 | exit 2 with the `show_batch` envelope and codes; never writes state or the cache beyond the read path of `todo-db show` |
| `todo-db validate` | changed | unchanged | validates schema 1–4 and v2 batch/receipt invariants | `E_SCHEMA` for unknown versions, `E_STATE` for invariant breaks |

### 4.3 New and changed refusal codes

| Code | Added or changed | Meaning |
|---|---|---|
| `E_BATCH_OWNER` | added | Caller is not the owner of the active batch; names the owner |
| `E_BATCH_HEAD` | added | Head lineage violation: unrecorded base, non-descendant, non-merge develop-merge, unrecorded commits |
| `E_FINAL_EVIDENCE` | extended | Also covers missing or mismatched `current_acceptance` |
| `E_SCHEMA` | extended | Schema 4 refused by old clients; batch mutation on a store below 4; v1 receipt under a v2 batch |

## 5. Consequences

- **Positive**: the leader is the only claimant of members, a dead leader is
  recoverable and audited, members can start from any recorded head and so merge
  in the integration head, fix commits and develop merges are recorded, the final
  PR binding survives review fixes, progress notes stop destroying receipts, and
  same-file evolution is accepted while the silent-revert hole stays closed by
  acceptance reruns. Existing 0.12.0 stores at schema 1 migrate in one commit.
- **Negative**:
  - Schema 4 is a hard cutover: every client with access to the state branch must
    be upgraded first; there is no negotiation (ADR 0007 amendment stands).
  - Current-acceptance reruns add verification time at final for each overlapped
    member. Accepted: the alternative is accepting reverts unverified.
  - Evidence is caller-attested (ADR 0003 §2.4): a leader can write passing
    acceptance evidence without running it. The tracker proves binding, not
    execution; this ADR does not change that boundary.
  - A `develop-merge` label exempts its files from scope accounting. The service
    verifies it is a real two-parent merge on the recorded head with exactly the
    stated file set, that `merged_parent` is an ancestor of the named `develop_ref`, and that the
    files also changed on the merged side; conflict resolutions in member files are
    not re-proven by todo-db, and `develop_ref` itself is the owner's assertion
    (audited: reason, files, `merged_parent`).
  - Owner-only claims depend on advisory identities (ADR 0003 §2.7): a worker
    sharing the owner's `--actor` is the owner.
  - Owner recovery needs the generation from the leader's ledger or Git history;
    if both are lost, a human must read the state branch.
- **Rejected alternatives**:
  - *Keep byte equality, add a bypass flag*: the flag would be the silent-revert
    hole. Rejected for recorded overlap.
  - *Serve the owner generation through `show_batch`*: turns the compare-and-swap
    into a stale read and contradicts the claim-generation rule. Rejected.
  - *Two cutovers (schema 3 then 4)*: the owner decision is one cutover on the new
    release; 0.12.0 never cuts over.
  - *Rebinding only through abort and re-register*: abort needs members claim-free
    and clears receipts, which defeats review fixes.
  - *Gating `update_item` on ownership*: edits are not claims; ADR 0003 §2.7 keeps
    them advisory.
- **Delivery**: one implementation change set cites this ADR, bumps the version
  and CHANGELOG, regenerates `scripts/mcp_snapshots/tools.json`, and releases only
  after Gate 1 (scratch remote, two real servers, 0.12.0 client refused with
  `E_SCHEMA`) and Gate 2 (`todo-db validate` against the consumer's state branch,
  read-only) per `docs/operations/release-gates.md`. The same change adds pointers to this ADR from ADRs 0006 (G2),
  0007 (prepared-batch amendment) and 0008 (batch ownership bullet).
