# ADR 0006: MCP as the Sole Agent Interface

- **Status**: Accepted
- **Date**: 2026-09-02
- **Context Item**: `mcp-interface-decisions`
- **Companion**: `docs/design/mcp-interface-migration.md` (implementation plan)
- **Relationship to prior ADRs**:
  - ADR 0003 — every *decision* stands. §2.10's rationale is superseded (G8).
    §2.5 vs. the shipped code is resolved (G7).
  - ADR 0005 — G1's non-goal is **amended** (G3): the credential-scoping
    boundary moves from the `todo-db` child process to the MCP server process.
    No other ADR 0005 or ADR 0004 decision is touched.

---

## Context

Autonomous coding agents drive `todo-db` through two mechanisms today:

1. A per-agent adapter. Only one exists — `@todo-db/pi-adapter`, ~560 lines of
   TypeScript (`integrations/pi/`) that re-implement a safety envelope (project
   discovery, environment allowlist, output cap, mutation serialization) around
   the `todo agent …` CLI.
2. A skill instruction layer. `.claude/skills/todo/` tells an agent to run
   `_project/scripts/todo <subcommand>` and to treat each subcommand's `--help`
   as its contract.

Both scale badly. Each new agent (Codex, Gemini, "muse", grok, Cursor,
Windsurf, Zed, Continue) needs either its own adapter or a skill port, and the
skill path normalises free-form shell execution and depends on the model
assembling correct command lines.

The Model Context Protocol (MCP) is the cross-agent standard for exposing typed
tools. A single MCP server reaches every MCP-speaking client with no per-agent
code. This ADR records the decision to make MCP the sole agent interface and the
boundaries that decision must respect. Backward compatibility is explicitly not
a goal; the `agent` CLI group and the Pi adapter are removed, not maintained.

---

## Decisions

### G1: MCP is the sole agent interface

`todo-db` ships one MCP server (`todo-db-mcp`, packaged as the `todo-db[mcp]`
optional dependency). It is a thin transport over the existing
`AgentWorkflow` / `TodoTracker` / `TodoDatabase` classes; no workflow, audit,
claim, or scope logic is re-implemented in the transport.

Removed once the server is proven (ADR-plan phase 0.6.0): the `agent` CLI
subcommand group, `_agent_instructions`, the Pi adapter and its npm package, the
`_project/scripts/todo` wrapper, `TODO_DB_PI_PRINCIPAL`, and the
`init-project --wrapper` / `refresh-wrapper` surface.

**Rejected — keep per-agent adapters or per-agent skill ports.** That is the
cost this decision exists to remove. **Rejected — MCP *plus* a full parallel
CLI.** Two mutation surfaces double the audit-contract and test surface for no
user; ADR 0003 §2.10 already declined a duplicate mutation channel.

### G2: A minimal non-agent CLI floor remains

Three consumers cannot be served by a model-driven tool call and keep a
`todo-db` CLI verb:

- **Bootstrap** — `init`, `init-project`, `migrate`, `doctor`. A server cannot
  create or migrate the database it needs before it runs.
- **CI and release gates** — `audit verify`, `export`, `restore`,
  `restore-legacy`, `import-yaml`. Spinning an MCP client in CI to groom a
  backlog is the wrong tool.
- **Human recovery and credentialed steps** — `verify-run` (G6), `rebaseline`
  (G6), `complete`, `finding sync` (the credentialed landing step),
  `config set`, `sweep-stale`.

The floor CLI adds **no new runtime dependency**. It keeps the
`TODO_DB_AUTH_CONTRACT=v2` two-way exit-code behaviour (exit 4 vs. exit 2) that
the parity conformance suite requires; the server sets `v2` in its own
environment, where per-call exit codes are vacuous.

**Rejected — zero CLI.** A daemon cannot bootstrap itself, and a release
pipeline should not depend on an agent runtime.

### G3: The server process is the credential-scoping boundary (amends ADR 0005 G1)

ADR 0005 G1's non-goal reads: "A credential is resolved for the capability the
operation requires and is passed to the `todo-db` child process only." A
long-lived server has no child process. This ADR amends that sentence:

- The server resolves a **capability-scoped credential per tool call**, using
  the same tool→`CredentialMode` mapping the CLI uses per command
  (`_mode_for`). A read-only tool call never resolves or holds a read-write
  credential.
- The server opens a **fresh database connection per tool call** and closes it
  when the call returns. It does not hold one standing read-write connection for
  the session.
- On `E_AUTH_REJECTED` mid-session the server resets the credential-provider
  cache and re-resolves, because ADR 0005's "retry in a fresh process"
  remediation is not an action a model can take.

The intent of ADR 0005 G1 is preserved: no agent holds ambient standing
read-write authority. What changes is only which process is the scoping unit.
Every other ADR 0005 decision (G2–G6) and all of ADR 0004 stand unamended.

**Rejected — one read-write connection for the server lifetime.** It gives every
session ambient write authority, the exact outcome ADR 0005 G1 rules out.

### G4: One dedicated worker thread owns all database and git work

FastMCP does not serialise tool calls. `sqlite3` connections are
thread-affine (`check_same_thread`), and `GitScopeEngine` — especially
`workspace_fingerprint()`, which reads every untracked file's bytes — must not
run on the stdio event loop.

The server runs all `TodoDatabase` and `GitScopeEngine` work on a single
dedicated worker thread. This serialises intra-process mutations (replacing the
Pi adapter's `SerializedQueue`), keeps every SQLite object on one thread, and
keeps stdio framing, pings, and cancellation responsive. Cross-process safety is
unchanged and still rests on `BEGIN IMMEDIATE` + `PRAGMA busy_timeout`.

### G5: Identity is explicit; the server never infers a principal from a session id

`default_actor()` returns the first of `TODO_ACTOR`, `CLAUDE_SESSION_ID`,
`CODEX_SESSION_ID`, `AGENT_SESSION_ID`, then `user@host` — i.e. it will use a
*session* id as the stable *principal*, breaking ADR 0003 §2.1 and §2.2.

The server resolves its principal from `--actor` / `TODO_DB_ACTOR`, or, when
unset, derives `mcp:<clientInfo.name>:<user>@<host>` from the MCP `initialize`
handshake — **unconditionally**. It never calls `default_actor()`. The session
id is a per-process UUID (or `--session`), passed on every `take` so a restarted
server re-adopts its own claim via same-principal adoption (ADR 0003 §2.2).

stdio transport inherits the CLI's local-process trust model. The Pi adapter's
client-side project-trust gate (`isProjectTrusted()`) is a client property and
is replaced by the client's own MCP-server registration approval.

### G6: The verification-execution and rebaseline boundary is preserved and moved out of the tool surface

ADR 0003 §2.4 stands. The `finish` **tool** is model-assert only: it requires a
current deterministic workspace-fingerprint attestation and rejects a stale
pass.

Verification execution is **not a tool at any profile**. It is a floor CLI verb,
`todo-db verify-run <id> --claim-token <t> --actor <principal>`, which previews
every stored command, runs the ladder once, re-checks scope, and **attests only**
— it does not complete the item, so the model's `finish` tool remains the
closer. `--actor` is required because the claim is held by the server's
principal, not the human's shell. `rebaseline` is likewise a floor verb with
`--actor`. `TODO_DB_ALLOW_HOSTED_VERIFY_RUN` is kept out of the server's
environment.

This does not create an absolute capability boundary — every MCP client also has
a shell tool. It removes *accidental* invocation and stops the tracker from
*normalising* free-form shell execution in its own instructions, which the skill
approach did.

**Rejected — a gated `run_verifications` tool.** A tool call is model-driven by
definition; gating it on an environment flag set once at server launch does not
make it human-initiated. MCP "elicitation" may revisit this when portably
supported.

### G7: `finish` retains the claim on a lint-gate failure (resolves ADR 0003 §2.5)

ADR 0003 §2.5 classifies lint findings as a **code failure** for which "the
claim is retained." The shipped `AgentWorkflow.finish` releases the claim and
its token before raising `E_LINT_GATE`. This ADR resolves the contradiction in
favour of the ADR 0003 decision: `finish` is fixed to retain the claim on a
lint-gate failure, so the model can repair the plan and retry within its lease.
Structural failures (missing dependencies, malformed definitions) still release
immediately per §2.5.

### G8: `next_action` is machine-readable; ADR 0003 §2.10's decision stands, its rationale is superseded

The `next_action` object returned by `next` / `take` / `progress` / `context`
names the next **tool** and its **arguments**, not a shell command string. A
`command` string is dual-emitted during the additive release for the
soon-removed adapter and skill, then dropped.

ADR 0003 §2.10 ("no separate JSON patch command") — the **decision** stands.
Lifecycle state moves only through dedicated verbs/tools. Its rationale's
"retaining existing CLI flags preserves canonical audit contracts" clause is
superseded: the flags are removed. `update_item` (a `--profile full` tool with
the same audited add/drop parameters the CLI `update` had) is the amend surface;
it is not a general JSON-patch mutation channel.

### G9: Tool surface — profiled, not one-per-verb and not one mega-tool

The server exposes grouped tools under two profiles:

- `agent` (default) — the six hot-path lifecycle tools (`next`, `take`,
  `context`, `progress`, `finish`, `release`) plus read-only queries
  (`list_items`, `show_item`, `ready`, `stats`, `deps`, `deferrals`, `export`,
  `check_scope`, `verify_list`, `lint`, `start_unit`, `claims`).
- `full` — adds planning (`create_item`, `update_item`, dependency and deferral
  verbs, `block`/`unblock`/`drop`), findings capture and triage (not
  `finding_sync`), and read/bootstrap admin (`init_project`, `config_get`).

`finding_sync`, `config_set`, `sweep_stale`, `migrate`, `complete`,
verification execution, `rebaseline`, `restore*`, `import-yaml`, and forensic
`audit` are never tools (G2/G6).

**Rejected — one tool per CLI verb** (~45 schemas injected every turn: context
bloat and tool-selection errors). **Rejected — a single `todo(action, …)`
mega-tool** (a large weakly-typed union where every argument is optional).

### G10: Delivery is split — additive `0.5.0`, destructive `0.6.0`

`0.5.0` ships the server, the error taxonomy, the connection and threading
model, the skill rewrite (via the external skill catalog), and the
`mcp-clients.md` registration guide, with the `agent` CLI and Pi adapter
**present but deprecated**. `0.6.0` deletes them and reshapes the CLI and the
release gates.

Between the two, three things must be proven: every named target (including
"muse" and grok) drives the stdio server end to end; BenchBox's parity
conformance command list is migrated in lockstep; the rewritten `todo` skill is
released through `skill-sync-skills.git`. If target support fails after `0.6.0`
the only recovery is a revert release — hence the split.

Under SemVer 0.y a breaking change bumps the minor. `1.0.0` is not claimed while
the load-bearing target-support assumption is unverified.

---

## Consequences

- Every MCP-speaking agent gets the full claim-coordinated workflow with no
  per-agent code. The ongoing cost of a new agent drops to a registration
  snippet.
- The tool schema, not a skill document, is the contract. Argument validation,
  the environment allowlist, and the output cap live once, server-side.
- `next_action` hands the model the literal next call, removing `--help`
  probing, wrapper indirection, shell-quoting, and prose parsing from the hot
  path.
- The connection-per-call model adds one credential resolution and one
  audit-head check per tool call. This is measured against the query-budget
  harness; it is the price of preserving ADR 0005 G1 and ADR 0003 §2.4.
- A long-lived server against a hosted (Turso/libSQL) primary is riskier than
  today's one-shot processes (`HostedConnection` has no reconnect). The server
  is local-SQLite-first; hosted is gated behind `--allow-hosted` and stays
  experimental per ADR 0003 §2.9.
- The `agent` CLI, the Pi adapter, and `_project/scripts/todo` are removed in
  `0.6.0`. Downstream consumers migrate to the MCP server or the floor CLI;
  there is no compatibility shim.
- BenchBox's `parity_conformance` command list and the `todo` skill in the
  external catalog become cross-repo release dependencies of `0.6.0`.
- The `E_LINT_GATE` claim-release behaviour changes (G7): finishing with lint
  findings now leaves the claim held.

## Amendment: a teaching skill is not a per-agent port

This record rejects "keep per-agent adapters or per-agent skill ports" because
each such port is a second implementation of the workflow that has to be
maintained per client and can drift from the server.

A skill that *documents* the MCP surface is not that thing. It adds no
mutation path, reimplements no logic, and is client-agnostic prose. The
distinction that matters is whether an artefact can be used to change tracker
state without going through a tool call. The rejected adapters could; a skill
cannot.

This repository therefore ships a `todo-db` skill, owned here rather than in an
external catalog, mirrored into `.claude/skills/`, `.codex/skills/`, and
`.gemini/skills/` so a clone is self-contained. It teaches the tool sequence,
the response envelope, and gate recovery, and it names `verify-run` and
`rebaseline` as human-only floor verbs.

The rejection above still stands for anything that adds a mutation surface.
