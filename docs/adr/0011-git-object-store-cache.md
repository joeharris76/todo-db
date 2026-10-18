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
  `FETCH_HEAD` (on git 2.29 or newer it is not written either).
  Concurrent processes therefore never race on remote-tracking refs, and
  every local ref is advertised as a "have", so only new objects
  transfer.
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
- **Maintenance.** Every fetch and write checks the store's slack (bytes
  beyond its largest pack, read from the object directory). Above 8 MiB a
  detached process runs `repack -A -d -l` and `prune --expire=1.hour.ago`,
  at most every 10 minutes (every minute after a failure). Above 16 MiB
  it runs inline whatever the cooldown, so write bursts and a failing
  background run are bounded and the caller pays. `-A` with the one-hour
  expiry keeps objects another process has just written but not yet
  referenced. A failure is recorded in `maintenance.error`, which
  `validate` reports.
- **Size bound, as measured.** Steady-state growth per commit is about the
  packed delta of the files it changes plus a fixed record that every Git
  commit carries: the commit object with its trailers, two tree deltas,
  an `index.json` delta and pack index entries. That record measured
  510 B per commit at 300 items and 900 B at 3,000 items. So the bound is
  about 2 times the bytes touched (the item's detail file plus its index
  entry) plus about 1 KiB per commit:
  - For edits of realistic size it is within 2 times on its own: 0.72
    times on a copy of BenchBox state (20 one-item edits, with the
    shipped maintenance), and 0.21 times on the test fixture.
  - For tiny edits it is not: a title-only change touches about 140 B
    and costs 3.6 to 6.4 times that. Meeting 2 times there would mean
    dropping history, which operation-ID reconciliation searches, or a
    new state format.

  The test suite asserts both forms.
- **Isolation.** Commands drop inherited variables that would redirect
  Git to another repository, index or identity (`GIT_DIR`,
  `GIT_INDEX_FILE`, `GIT_AUTHOR_*` and similar), and drop
  `GIT_CONFIG_PARAMETERS` (`git -c` from a parent git process). Every
  command re-asserts `core.hooksPath`, `commit.gpgSign`, `gc.auto` and
  `maintenance.auto` through `GIT_CONFIG_COUNT`, after any inherited
  entries, so neither config files nor inherited command-level config can
  turn hooks, signing or automatic gc back on. Inherited entries for
  other keys, such as credentials, still apply. Git does not pass
  `GIT_CONFIG_COUNT` to a local-path remote's receive-pack, so a remote's
  own hooks still run.
- **Locking.** Ordinary use holds a shared `flock` on `store.lock` and
  creates the store under it if it is missing, so a holder never sees a
  namespace without a repository. Maintenance also holds the shared lock.
  After waiting, a lock is retaken if its file was replaced, as `cache
  prune` does when it moves a namespace away.
- **Damage.** A store that fails locally is replaced under the exclusive
  lock, and only if its ID still matches the one the failing operation
  saw. The fresh store refetches. A tip whose objects are entirely
  missing is simply fetched again; other damage, including history that
  cannot be searched, triggers that rebuild. A history search that cannot
  run is never treated as proof that an operation is absent: a mutation
  rebuilds and searches again, and reconciliation reports `unknown`.
- **Namespace.** The store's namespace hashes the resolved remote
  location, so one relative local path used from two directories cannot
  share a store. For URL remotes it equals the legacy namespace.
- **Legacy cleanup** is an explicit command, `todo-db cache prune`, never
  part of a read. It handles each namespace under its exclusive lock,
  taken without waiting, so one in use is skipped. It removes legacy
  `revs/`, `last` and temporary pointer files once that namespace's
  store has a tip. Given the project's state branch (flags or a
  discovered config), it also clears a local remote's legacy namespace,
  which hashes differently from its store. It also removes folders from
  before namespacing, namespaces unused for 30 days (at least one day),
  staging and trash older than an hour, and leftover top-level trash,
  then repacks each store.
- **Git version.** Requires git 2.28 or newer, as bootstrap already did
  (`init -b`). The version is probed once per process, and an older git
  fails with a clear error. The temporary-repository path is not kept
  as a fallback, which avoids maintaining two write paths.
  `GIT_CONFIG_COUNT`, used to re-assert the protected settings, needs
  git 2.31; older git still applies the store's own config, which
  outranks global config but not inherited command-level config.

## Rejected alternatives

- **Keep two snapshots per namespace** (evict the rest outside reads,
  plus a one-time cleanup). This also meets a retained-growth target, and
  it is the smaller change. It was not chosen because each new tip would
  still fetch the whole tree and write about 30 MiB of files. Measured on
  a copy of BenchBox state, a read after a new tip took 1.7 s, against
  0.37 s with the store. Each mutation would also keep fetching the
  branch's full history. A safe sweep needs the same locking and trash
  renames as the store.
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
  history: about 14 MiB, and a first `validate` takes 3.7 s for BenchBox
  against 2.8 s on 0.9.1, with a 300 s timeout. After that, each call
  transfers only new objects.
- Writes no longer re-fetch the full history into a temporary
  repository. The test suite runs about 3 times faster.
- Users reclaim legacy snapshot folders by running `todo-db cache prune`
  once. `validate` prints that hint while a legacy cache remains.
- Remote spellings that differ only cosmetically (with and without
  `.git`) still get separate namespaces. `cache prune` expires the unused
  one.
