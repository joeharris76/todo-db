# ADR 0011: Git Object Store as the Local State Cache

- **Status**: Accepted
- **Date**: 2026-10-17
- **Amends**: ADR 0007 (JSON/Git tracker): offline reads and the local
  snapshot cache

## Context

Reads cached every state-branch tip they saw as a full snapshot under
`<cache>/<namespace>/revs/<sha>/`: an `index.json` plus one file per
item. Nothing removed old snapshots, and only the current tip and the
rev named by a `last` pointer were ever read back. Against BenchBox
production state (about 3,300 items) each snapshot took about 30 MiB on
disk, so the cache grew by about 30 MiB per new tip, by up to about
3 GiB a day, and reached 41 GB on one machine.

Each mutation also fetched the state branch's whole history into a new
temporary repository, so its transfer and disk writes grew with the
branch.

Measured on BenchBox `todo-state` (5,114 commits) on 2026-10-17:

- The whole branch history packs to 14.2 MiB, less than half of one
  cached snapshot.
- Each commit touches about 4.4 to 5 KB of item JSON. Its marginal cost
  once packed is about 1.8 KB, or 0.36 times the bytes it touches.
- Every commit rewrites the 900 KB `index.json` and the 243 KB `items`
  tree. Unpacked, a fetched tip therefore costs 150 to 300 KB until a
  repack folds it into deltas. A full `git repack -ad` of the branch
  takes 0.64 s.
- Reading a whole tip from objects (`ls-tree -r` plus one
  `cat-file --batch`) takes 0.14 s.

## Decision

Replace the per-revision snapshot folders with one bare Git repository
per state branch, `<cache>/<namespace>/store.git`, used by reads,
mutations, history, reconciliation and restore (`src/todo_db/git_store.py`).

- **Incremental fetch by URL.** The store has no configured remote. Each
  call fetches the URL with an explicit refspec onto its own ref,
  `refs/todo-db/in/<uuid>`, deletes that ref when done, and never reads
  `FETCH_HEAD`. Concurrent processes therefore never race on
  remote-tracking refs, and every local ref is advertised as a "have",
  so only new objects transfer.
- **Snapshots from objects.** Reads build the snapshot from Git objects.
  No checkout or snapshot file is written. Checkout and object loads
  share one assembly (`store.assemble_snapshot`), so both accept exactly
  the same trees, symlink refusals included.
- **Commits by plumbing.** A mutation hashes only the state files whose
  bytes or mode changed, builds the tree in a temporary index file, and
  runs `commit-tree` with the message normalized as `git commit -m`
  would. Trees and commit bodies match the previous checkout path
  exactly; a parity test pins this.
- **One shared ref, moved by CAS.** `refs/todo-db/tip` replaces the `last`
  pointer. It moves forward by compare-and-swap and skips a tip that is
  an ancestor of the stored one, so racing processes cannot move it
  backwards. A tip that does not descend (a divergent rewrite of the
  remote branch) still replaces it. A rewind to an ancestor looks the
  same as a slow process, so it is not followed; branch rewrites are
  outside the protocol (ADR 0007).
- **Offline reads** serve the store's tip. Until a store has a tip, they
  fall back to a legacy `revs/<last>` snapshot.
- **Size bound.** Once unpacked objects exceed 8 MiB beyond the largest
  pack, and at most every 10 minutes, a detached process runs
  `repack -A -d -l` and `prune --expire=1.hour.ago` under a non-blocking
  lock. `-A` with the one-hour expiry keeps objects that another process
  has just written but not yet referenced. In steady state the store
  grows by about the packed delta of each commit: 0.21 to 0.58 times the
  bytes of the items touched, within the target of 2 times. The test
  suite asserts the 2-times bound.
- **Isolation.** Commands drop inherited variables that would redirect
  Git to another repository, index or identity (`GIT_DIR`,
  `GIT_INDEX_FILE`, `GIT_AUTHOR_*` and similar). The store's own config
  disables hooks, signing, automatic gc and commit-graph writes, so user
  configuration cannot change a commit.
- **Damage.** A store that fails locally is moved aside under an
  exclusive lock, but only if its ID still matches the one that failed.
  It is rebuilt once by refetching. Missing objects are simply fetched
  again.
- **Namespace.** The store's namespace hashes the resolved remote
  location, so one relative local path used from two directories cannot
  share a store. For URL remotes it equals the legacy namespace.
- **Legacy cleanup** is an explicit command, `todo-db cache prune`, never
  part of a read. It removes legacy `revs/`, `last` and temporary pointer
  files once that namespace's store has a tip. It also removes folders
  from before namespacing, namespaces unused for 30 days, and leftover
  trash, then repacks each store.
- **Git version.** Requires git 2.28 or newer, as bootstrap already did
  (`init -b`). `GIT_CONFIG_COUNT`, which keeps global hooks out of store
  creation, needs git 2.31; on older git a global `reference-transaction`
  hook can still run during `git init`.

## Rejected alternatives

- **Evict old snapshots in `read()`.** Bounding the folders would stop
  the growth, but each new tip would still write about 30 MiB and fetch
  the full tree. The first sweep would delete about 40 GiB inside an MCP
  call. A safe sweep also needs locking and trash renames; the store
  needs those too and removes the folders altogether.
- **A persistent shared worktree.** Rejected in `docs/measurements.md`
  for its shared mutable scratch state. The store has no worktree: it
  shares only immutable objects and one ref moved by CAS.
- **A schema 4 state format** (a split index, a fanned-out `items`
  tree), so that even an unpacked commit stays within 2 times the items
  it touches. It would need a cutover across every client. The amortized
  bound needs no format change. Revisit only if the bound must hold
  before maintenance runs.

## Consequences

- The first read or write for a namespace fetches the branch's whole
  history: about 14 MiB and 3 s for BenchBox, with a 300 s timeout. After
  that, each call transfers only new objects.
- Writes no longer re-fetch the full history into a temporary
  repository. The test suite runs about 3 times faster.
- Users reclaim legacy snapshot folders by running `todo-db cache prune`
  once. `validate` prints that hint while a legacy cache remains.
- Remote spellings that differ only cosmetically (with and without
  `.git`) still get separate namespaces. `cache prune` expires the unused
  one.
