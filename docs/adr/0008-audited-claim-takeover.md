# ADR 0008: Audited Claim Takeover for Unresumable Sessions

- **Status**: Proposed
- **Date**: 2026-09-19
- **Supersedes**: §2.2 of ADR 0003 (cross-principal takeover only after expiry, no forced surface)

---

## 1. Context and Problem Statement

ADR 0003 §2.2 reserves cross-principal claim acquisition for expired
leases. When the holding session cannot resume (process gone, budget
exhausted, identity unrecoverable), the task waits out the full lease —
up to 72 hours — with no recourse: every live-claim path demands the
original generation, which died with the session. Liveness of the holder
cannot be proven from inside the protocol, and heartbeat detection would
reintroduce time under another name.

## 2. Decision

A different principal may take over a live foreign claim immediately
through an explicit, audited `takeover` operation that amends — not
reinterprets — the §2.2 rule:

- **Caller-bound comparison**: the caller names the exact holder it
  inspected; any holder change (handover, renewal, release) fails
  `E_CONFLICT`. The full claim image is pinned across publication
  retries, restoring one-winner semantics for well-behaved agents.
- **Asserted reason**: the caller states why the session cannot resume.
  Reasons are bounded, single-line, validated attribution — an assertion
  under the cooperative model, not a proof of death.
- **Fresh claim**: new generation, zeroed renewals, fresh lease; the
  displaced image goes stale exactly as in re-adoption.
- **Batch ownership follows the item**: batches owned by the displaced
  holder that contain the taken item transfer to the taker, or prepared
  work strands permanently with no expiry to wait out.
- **Fail-closed attribution**: takeover requires a session; sessionless
  callers are refused rather than recorded-skipped. Same-worker and
  unclaimed cases are redirected to `take`.
- **Dual-surface audit**: session entry (`takeover`, displaced holder,
  reason) plus commit trailers and a holder-naming summary.

Same-principal adoption (§2.2, first half) is unchanged.

## 3. Consequences

- **Positive**: dead sessions stop blocking tasks for hours; every
  transfer names who, whom, and why on both the item log and the commit.
- **Negative**: claim theft becomes a single audited call instead of
  silent actor-string reuse. Accepted: the threat class pre-exists under
  advisory identities (§2.7), and attribution converts it from silent to
  accountable. Silent spoofing itself is not removed — that needs
  authentication this project does not have.
