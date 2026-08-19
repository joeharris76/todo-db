# ADR 0001: Audit Chain Integrity Guarantees at Database Open

- **Status**: Accepted
- **Date**: 2026-08-18
- **Context Item**: `opt-audit-open-policy-adr`

---

## 1. Context and Attacker Model

`todo-db` maintains an append-only cryptographic hash chain (`sha256-chain-v2`) in the `events` table, with the current sequence and head digest mirrored in the `audit_head` singleton table.

### 1.1 Attacker Model and Trust Boundaries

The system operates across two primary environments with distinct trust boundaries:

1. **Local Embedded SQLite**: Single developer workstation. The local file is trusted; threat model focuses on unintentional corruption, concurrent worktree collisions, and git merge anomalies.
2. **Hosted Shared Backend (LibSQL / Turso over HTTPS)**: Multi-tenant, remote database shared across team members and automated agent workers.
   - **Threat Actors**:
     - *Compromised / Rogue Remote Actor*: An actor with database credentials attempting to retroactively rewrite task history, alter completed code scopes, or forge completion events.
     - *Direct SQL Manipulation*: An administrator or unauthorized user altering intermediate event rows directly via raw SQL without advancing the valid hash chain.
     - *Network / Transport Intermediary*: An adversary attempting to inject, drop, or substitute event rows in transit.
     - *Concurrent Race Conditions*: Multiple workers appending events simultaneously without proper linearization.

### 1.2 Definition of Tamper-Evident

In this system, *tamper-evident* means:
- **Historical Immutability**: Any modification to the sequence, timestamps, actor IDs, actions, details, or predecessor pointers of existing events alters subsequent digests and mismatches the verified chain.
- **Head Continuity**: Any new event appended to the database must strictly chain off the exact previous `head_hash` at sequence `head_seq + 1`.
- **Detection Reliability**: A client can deterministically verify that the event sequence from `1..N` forms an unbroken, deterministic SHA-256 chain matching the recorded head.

---

## 2. Evaluation of Architectural Options

### Option A: Full Verification on Open (Status Quo Baseline)

In Option A, every invocation of `TodoDatabase.open()` queries `SELECT * FROM events ORDER BY seq` and recomputes all SHA-256 event hashes from `seq = 1` to `N` before returning the database handle.

- **Security Guarantees**: Complete, instantaneous detection of any past tampering before any read or write operation executes.
- **Performance Cost**:
  - Statements: $O(1)$ query returning $O(N)$ rows.
  - Latency: $O(N)$ network payload transfer and CPU hashing time. For a database with 10,000 events over HTTPS, open latency degrades by hundreds of milliseconds to seconds on every CLI command.
- **Assessment**: Unscalable for hosted shared databases with growing event histories.

---

### Option B: Head-Only Check on Open with Tiered Verification (Recommended)

In Option B, `TodoDatabase.open()` performs an $O(1)$ head consistency probe (`SELECT head_seq, head_hash FROM audit_head`) ensuring the head singleton exists, is well-formed, and matches the latest event sequence (`SELECT seq, event_hash FROM events ORDER BY seq DESC LIMIT 1`).

Full event chain verification is tiered:
- Executed on-demand via `todo-db verify-audit` (or `todo audit`).
- Enforced at critical lifecycle gates: `todo complete`, `todo export`, and pre-merge CI checks.

- **Security Guarantees & Detection Gaps**:
  - Guarantees immediate detection of head desynchronization, unlinked appends, and missing head metadata on every open.
  - *Documented Detection Gap*: Retroactive modification of an intermediate event ($1 < k < N$) where the head is left unchanged is not detected during `open()`; it is detected as soon as a lifecycle gate (`complete`, `export`, `verify-audit`) runs the full chain walk.
- **Performance Cost**: $O(1)$ query overhead on open (1-2 lightweight queries), eliminating latency scaling with history length.

---

### Option C: Extend Local Embedded Replica to Read-Only Commands

Option C maintains an embedded SQLite replica for both read-only and write commands, synchronizing changes via LibSQL background sync and running the full audit walk exclusively over the local SQLite replica.

- **Security Guarantees**: Preserves full verification on open while keeping all query execution local.
- **Performance Cost**: Full walk runs at microsecond SQLite speeds, but introduces local disk footprint, startup sync latency, and complexity around replica state synchronization across ephemeral worktrees.
- **Assessment**: High architectural complexity and introduces replica synchronization overhead.

---

### Option D: Persistent Background Daemon / Process

Option D runs a persistent local background daemon maintaining an open connection to the database, streaming new events, and incrementally verifying digests as new events are committed.

- **Security Guarantees**: Near real-time incremental verification with zero open penalty for CLI commands querying the local daemon.
- **Performance Cost**: Requires daemon lifecycle management (start/stop/restart/pidfiles), IPC protocol, and failure recovery mechanisms.
- **Assessment**: Over-engineered for a command-line developer utility.

---

### Option E: Signed and Merkle Checkpoints

Option E introduces periodic Merkle tree root snapshots or Ed25519-signed checkpoints recorded every $K$ events. Open checks verify only the latest checkpoint signature and events since the last checkpoint ($O(N \bmod K)$).

- **Security Guarantees**: Cryptographically proofs historical integrity in $O(1)$ / $O(\log N)$ steps.
- **Performance Cost**: Requires key management infrastructure for signing actors and schema additions for checkpoint storage.
- **Assessment**: Excellent future evolution path if event logs grow to hundreds of thousands of entries.

---

## 3. Decision and Implementation Policy

### 3.1 Chosen Decision

We adopt **Option B: Head-Only Consistency Check on Open with Mandatory Lifecycle Verification Gates** as the default policy.

1. **Default Open Behavior**:
   - `TodoDatabase.open()` validates database identity and performs an $O(1)$ audit head consistency check.
   - For write operations, event insertion atomically updates `events` and `audit_head` within the write transaction.
2. **Lifecycle Enforcement**:
   - Full chain verification (`verify_audit()`) is mandatory during `todo complete <id>` (preventing task closure on a tampered database), `todo export`, and `todo-db verify-audit`.
3. **Configuration Opt-In**:
   - Setting `TODO_DB_AUDIT_OPEN_POLICY=full` enables full audit verification on every open for high-assurance environments.
   - Setting `TODO_DB_AUDIT_OPEN_POLICY=head` (default) enables the fast $O(1)$ head check.

---

## 4. Consequences and Next Steps

- Unblocks `opt-audit-head-check-on-open` to implement the head check and configurable policy in `src/todo_db/database.py`.
- Enables sub-millisecond open latency on local SQLite and eliminates the $O(N)$ data transfer bottleneck on hosted LibSQL backends.
