# ADR 0003: Claim-Coordinated Agent Workflow Contract

- **Status**: Accepted
- **Date**: 2026-08-18
- **Context Item**: `agent-workflow-decisions`

---

## 1. Context and Problem Statement

Autonomous coding agents (e.g., Pi, Claude, Antigravity) interact with `todo-db` to select work items, update work units, verify progress, and complete tasks. In previous iterations, agents operated either through verbose multi-command CLI workflows with uncoordinated claims or risked race conditions and state corruption across session restarts.

This architecture decision record establishes the contract for the streamlined, claim-coordinated agent workflow.

---

## 2. Core Decisions

### 2.1 Stable Principal vs. Session Attribution
- **Stable Principal in `claimed_by`**: Identifies the responsible worker/entity (e.g., `pi-agent`, `user@host`, or configured actor name). It survives agent process restarts and subagent handoffs.
- **Session Attribution in `claimed_session`**: Records the ephemeral session identifier (e.g., `PI_SESSION_ID` or UUID).
- **Claim Token in `claim_token`**: A cryptographic random token generated upon every claim acquisition. Used for generation checking to prevent stale writes.

### 2.2 Same-Principal Adoption vs. Audited Cross-Principal Takeover
- **Same-Principal Adoption**: If an agent restarts with the same stable principal (`claimed_by`), it can adopt or resume its active claim without releasing or re-queuing.
- **Cross-Principal Takeover**: If a lease is expired (`_lease_expired` is True) or forced by an authorized operator, acquiring the item by a different principal emits an audited `claim` event detailing the previous holder.

### 2.3 One Live Claim per Agent Workflow
- An agent workflow instance is constrained to hold at most **one active claim** at any time.
- Attempting to take a new item while holding an active claim returns an advisory error pointing to the existing claim or requiring explicit release.

### 2.4 Verification Execution and Model Trust Boundary
- Stored verification commands in a shared database represent untrusted shell execution.
- Models may assert readiness (`agent finish --model-assert`), but stored shell commands (`verify --run`) must not be triggered implicitly by untrusted model tools on hosted databases unless explicitly authorized by human configuration (`TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1`).
- The agent finish gate verifies:
  1. Clean linter results.
  2. Verified work units.
  3. Scope adherence (no modified files outside `only_modify` or inside `do_not_modify`).
  4. Unbroken cryptographic audit chain.

### 2.5 Structural-Gate Release vs. Code/Verification Retention
- **Structural Failures** (missing item dependencies, malformed task definitions, unresolvable project identity): The claim is released immediately, and exact human remediation commands are returned to prevent runaway agent loops.
- **Code/Verification Failures** (syntax errors, test failures, lint findings, scope violations): The claim is **retained**, enabling the agent to inspect the error, adjust code, and retry within its lease window.

### 2.6 Git Baseline Divergence and Root-Safe Paths
- Changed-file calculation uses root-pinned, NUL-delimited Git commands (`git diff --name-only -z`, `git status --porcelain -z`).
- Handles file creations, modifications, deletions, and renames (tracking both old and new paths).
- Paths are normalized relative to repository root, avoiding cwd-dependent path ambiguities.

### 2.7 Advisory-Not-Authenticated Claim Semantics
- Claims provide cooperative concurrency control, preventing accidental collisions between well-behaved workers.
- The `--actor` flag is advisory and not a cryptographic identity boundary. The database connection credentials (`TODO_DB_AUTH_TOKEN`, TLS) enforce backend access control.

### 2.8 No Background Heartbeat in v1
- v1 relies on deterministic lease TTLs rather than background daemon heartbeats.
- Progress updates (`agent progress`) refresh the lease timestamp atomically upon milestone completion.

### 2.9 Local vs. Hosted Support Matrix
- **Local Embedded SQLite**: Full support for single-user workflows and worktree concurrency.
- **Direct Hosted Primary (LibSQL / Turso)**: Full support for transactional operations (`BEGIN IMMEDIATE`, audit chaining, direct primary mutations).

---

## 3. Consequences

- **Positive**:
  - Deterministic agent lifecycle with single-command `take`, `context`, `progress`, `finish`.
  - Zero possibility of agent hoard-locking multiple items.
  - Safe restart and crash recovery without orphaned lockouts.
- **Negative**:
  - Requires schema migration for `claimed_session` and `claim_token` on `items` table.
