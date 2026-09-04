# ADR 0002: Hosted Backend Architecture & Replica Evaluation

- **Status**: Decided (Direct Primary Connection Adopted)
- **Date**: 2026-08-18
- **Context Items**: `opt-hosted-latency-measurement`, `opt-replica-removal`

---

## 1. Executive Summary & Problem Context

Prior to this optimization cycle, hosted read-write mode used local embedded SQLite replicas with sync hooks. While embedded replicas cached reads locally, they introduced major operational drawbacks:
1. **Lock contention across worktrees**: Embedded replica files required cross-process file locks (`_replica_lock` via `fcntl`), failing on NFS, CIFS, and containerized multi-agent environments.
2. **Sync complexity**: Two-way sync failures during transient network errors could leave local replicas desynchronized.
3. **High query volume on unoptimized paths**: Commands like `ready` and `lint --all` issued 20 to 200+ SQL queries, making WAN latency unacceptable without local caching.

---

## 2. Benchmark Findings & Query Optimization Impact

With the completion of query optimizations across the codebase:
- `ready_items()` collapsed from $1 + N$ queries to a single $O(1)$ SQL query with `NOT EXISTS`.
- `load_item_snapshots()` collapsed from $1 + 8N$ queries to 11 bulk queries across child tables.
- `_check_audit_head` reduced verification on open to a single atomic $O(1)$ query.

### 2.1 Concurrency & Isolation
- Tested in `tests/test_hosted_latency.py:test_two_process_hosted_claim_race`, direct transactions with `BEGIN IMMEDIATE` / `isolation_level=None` guarantee that claim races against a shared primary result in exactly **one winner** and an immediate rejection for the losing process without stale-cache read windows.

---

## 3. Final Decision: Direct Primary Connections

### Outcome:
- **Adopt Direct Primary Connections for Hosted Mode**: All hosted connections connect directly to the primary Hrana endpoint.
- **Retire Embedded Replica Layer**: The embedded replica path, `_replica_lock`, and `fcntl` dependencies are removed.
- **Rationale**: Single-query commands require only 1 round-trip over HTTPS/Hrana. Direct connections eliminate local state corruption, file-locking failures in multi-agent environments, and synchronization complexity while maintaining clean transactional semantics.
