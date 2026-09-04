# Design plan: MCP as the sole agent interface for `todo-db`

- **Status**: Approved (basis for ADR 0006 and the mcp-migration batch)
- **Date**: 2026-09-02
- **Revision**: 2 (integrates adversarial review; splits delivery into 0.5.0 additive / 0.6.0 destructive)
- **Companion decision record**: ADR 0006 (write on approval)
- **Supersedes on acceptance**: the Pi adapter contract; the `_project/scripts/todo`
  wrapper contract; ADR 0003 §2.10 *rationale* (the decision stands — see §17)

---

## 1. Goal and non-goals

### Goal

Expose the `todo-db` tracker through a single **Model Context Protocol (MCP)**
server so every MCP-speaking coding agent — Claude Code, Codex, Pi, Gemini,
"muse", grok, Cursor, Windsurf, Zed, Continue — gets the claim-coordinated
workflow with **zero per-agent adapter code**. Replace the hand-maintained Pi
TypeScript adapter and the skill-driven "run `_project/scripts/todo …`" pattern
with typed tool calls.

Two questions drive the design:

1. **Cleaner boundary.** A skill document that tells an agent to run
   `todo agent take <id> --session <x>` normalises free-form shell execution and
   depends on the model assembling correct command lines. An MCP tool schema
   *is* the contract: the server fixes the operation set, validates every
   argument, and there is no shell string for the model to mutate. Verification
   execution and `rebaseline` have **no tool** at any profile. This removes
   *accidental* invocation and the normalisation of shell use — it is not an
   absolute capability boundary, because every listed client also has a shell
   tool (§7).
2. **Lower ceremony and fewer tokens.** Replace `--help` probing, wrapper
   indirection, shell-quoting, and prose parsing with schemas loaded once,
   structured arguments, compact JSON responses, and a machine-readable
   `next_action` that names the literal next tool call.

### Non-goals

- **Backward compatibility is not a goal.** The `agent` CLI group, the Pi
  adapter, and the wrapper are removed (in 0.6.0), not carried forever.
- Remote / multi-tenant hosting. stdio-only here; HTTP is a later additive
  decision (§12).
- A cross-project "workspace" server. One server instance = one project
  boundary = one worktree (§4).
- **Hosted (Turso/libSQL) as the MCP server's default backend.** Deferred (§M4 /
  §12): the server is local-SQLite-first; hosted requires an explicit
  `--allow-hosted` flag and stays experimental per ADR 0003 §2.9.

---

## 2. Prior art

| Source | What it gives us | Decision |
| --- | --- | --- |
| `src/todo_db/agent.py` — `AgentWorkflow` | The hot-path service (`next`, `take`, `context`, `progress`, `finish`, `release`, `adopt`, `rebaseline`), returning structured dicts. `__init__(tracker, repo_root)` — takes a path, builds its own `GitScopeEngine`. | **Extend.** The server is a thin transport over this class. Add a `git_engine`/`repo_root` injection point (review §M9). |
| `src/todo_db/tracker.py` — human lifecycle | `create_item`, `update_item`, `claim`, `start_unit`, `done_unit`, `defer`, `promote_deferral`, `dismiss_deferral`, `complete`, `drop`, `block`, `unblock`, `check_scope`, `lint`, `run_verification`, `sweep_stale`, `list_items`, `stats`, `get_config`/`set_config`, `attest_verifications`, `rebaseline_scope`. | **Wrap the read-only and no-shell ones as tools; route shell + destructive ones to the CLI floor (§9).** |
| `src/todo_db/backends.py` + `database.py` | `_mode_for()` selects `CredentialMode` per command; `resolve_credential` resolves a **capability-scoped** credential per child process; `TodoDatabase.open` **migrates on any non-READ_ONLY open**; `_PROVIDER_CACHE` memoises for process life; `_connect_sqlite` is `check_same_thread=True`; cross-process safety = `BEGIN IMMEDIATE` + `busy_timeout=5000`. | **These dictate the server's connection model (§8). Not "unchanged".** |
| `integrations/pi/src/*.ts` | Reference safety envelope: project-root discovery, env allowlist, 16 KB cap **on the tool path only** (64 KB elsewhere), mutation `SerializedQueue`, single `todo_db` tool with an `action` enum, `isError: !gate` (protocol error vs in-band gate), client-side `isProjectTrusted()`. | **Supersede.** Re-implement server-side in Python — except the trust gate, which is a *client* property and is replaced by client-side registration approval. |
| `docs/adr/0003` | Claim/session/token model, verification trust boundary, structural-vs-code release, one-live-claim, root-pinned git paths, §2.9 hosted-experimental, §2.10 no-JSON-patch. | **Keep every *decision*.** §2.10's rationale is superseded (§17). §2.5 (lint = code failure, claim retained) currently contradicts the code (§S6) — ADR 0006 resolves it. |
| `docs/adr/0004`, `docs/adr/0005` | Headless credential model; **"no agent receives ambient read-write tracker authority"** (0005 G1 non-goal); credential resolved per capability per child process. | **ADR 0006 must amend 0005 G1 explicitly** — a long-lived server changes this (§8, §B1). |
| `scripts/parity_conformance.py` + `scripts/parity_allowlist.json` | Cross-implementation contract vs **BenchBox**'s `todo_db_standalone_compat.py` `COMMANDS`; `diff_help`, `diff_exit_codes` (codes 0/1/2/4 + `E_CLAIM_STALE`/`E_LINT_GATE`/`E_SCOPE_GATE`/`E_VERIFY_GATE` must stay in `errors.py`), `diff_ddl`. | **Extend for the floor; coordinate the deletions with BenchBox (§13, §B6).** DDL + exit-code halves stay intact. |
| `skill-sync.yaml` / `skill-sync.lock` / `docs/operations/skill-deployment.md` | `install_mode: mirror`, `tracked: true`; the `todo` skill comes from the pinned external `skill-sync-skills.git` catalog; "never edit a generated target as source of truth"; lock + all three trees change in one commit; CI runs `skill-sync verify`. | **The skill rewrite is a cross-repo catalog release (§13, §S1), not an in-repo edit.** |
| `_project/scripts/todo` | Tool resolution order (`TODO_DB_TOOL` → PATH → sibling), `TODO_DB_AUTH_CONTRACT=v2`, exit-4 remediation. | **Fold resolution into the server launcher; keep `v2` semantics on the floor CLI (§M3).** |
| MCP Python SDK (`mcp`, FastMCP) | stdio server, tool registration, `initialize`/`clientInfo`, `structuredContent`+`outputSchema`, prompts, resources. | **Adopt as an *optional* dependency: `todo-db[mcp]` (§S8).** |

No existing full MCP server for this tracker exists to extend; this is new
infrastructure on the existing `AgentWorkflow` core.

---

## 3. Target architecture

```
                one process per (project boundary × worktree × agent session)

  Agent (MCP client) ──stdio JSON-RPC──▶ todo-db-mcp
                                            │
       server.py     lifespan: resolve target (§4), open READ_ONLY, schema-check only (§8/B2)
       identity.py   actor resolution — mandatory, never falls to default_actor() (§8/B5)
       dbpool.py     per-tool: resolve capability-scoped credential, open connection,
                     run in ONE dedicated worker thread, close (§8/B1/B3)
       envelope.py   ok/err shaping, error taxonomy (§M2), byte cap + paging fallback (§11)
       tools_work.py    next take context progress finish release
       tools_plan.py    create_item update_item add_dependency defer promote_deferral
       tools_query.py   list_items show_item ready stats deps deferrals export
                        check_scope verify_list lint start_unit
       tools_findings.py finding_create(draft) finding_list finding_show finding_triage
                        finding_link finding_promote finding_dismiss           (NOT finding_sync — §S3)
       tools_admin.py   init_project config_get                               (read/bootstrap only)
       prompts.py       todo/workflow            (additive; not portable — §S2)
       resources.py     todo://instructions, todo://item/{id}, todo://ready    (portable path)
       get_instructions (tool)                                                (portable fallback for the prompt)
                                            │
                                            ▼
                        AgentWorkflow / TodoTracker / TodoDatabase  (minimal changes: git_engine
                        injection; next_action dual-emits command + {tool,arguments} in 0.5.0)
                                            ▼
                              local SQLite  (hosted behind --allow-hosted, deferred)
```

New package `src/todo_db/mcp/`: `__init__.py`, `__main__.py`, `server.py`,
`identity.py`, `dbpool.py`, `envelope.py`, `tools_work.py`, `tools_plan.py`,
`tools_query.py`, `tools_findings.py`, `tools_admin.py`, `prompts.py`,
`resources.py`.

```toml
[project.optional-dependencies]
mcp = ["mcp>=<pinned>"]        # confirm the SDK's Python floor before pinning (§S8)

[project.scripts]
todo-db-mcp = "todo_db.mcp.server:main"   # fails with an install hint if `mcp` absent
todo-db     = "todo_db.cli:main"
```

---

## 4. Project and database resolution

**Problem.** The CLI walks up from `cwd` for `.todo-db/config.json`. An MCP
server is launched with a client-controlled `cwd`.

**Decision.** Resolve once at startup, in this precedence, and **pin for the
process lifetime**:

1. `--config` / `--db` / `--repo-root` launch flags.
2. `TODO_DB_CONFIG` / `TODO_DB_URL` / `TODO_DB_PATH` env.
3. Upward discovery from `--repo-root` (default: server `cwd`).

**One server = one project = one worktree.** Worktree concurrency runs one
server per worktree. A multi-project server is out of scope.

**Per-client reality (was asserted "identical"; corrected per §S2):**

- **Claude Code** — `.mcp.json` lives at the project root and the server
  launches from it. **Omit `--repo-root`; default to cwd.** `${workspaceFolder}`
  does **not** expand in Claude Code (`${VAR}` / `${VAR:-default}` are
  environment-only).
- **Codex** — `~/.codex/config.toml` is **user-global**; a single
  `[mcp_servers.todo-db]` cannot carry a per-project `--repo-root`. **Open item
  (§15):** confirm project-scoped MCP config; if absent, Codex resolves from cwd
  (accepting the §4 precedence's discovery tier) or uses the floor CLI.
- **Cursor / Windsurf / Zed / Continue** — each has an `mcpServers` block;
  `${workspaceFolder}`-style expansion varies. Ship a per-client snippet, not
  one template.
- **Pi** — replace `@todo-db/pi-adapter` with Pi's native MCP config.
- **muse / grok** — **unverified** they speak MCP stdio (§15). Blocks 0.6.0.

`.todo-db/config.json` still supplies `project_id`, `repository`, `db`. The
`wrapper` key is now ignored — see §9 migration note.

**Rejected:** per-call `repo_root` arguments (re-introduces path ceremony + an
injection surface).

---

## 5. The tool surface

MCP clients inject every tool's schema every turn, so tool count is the dominant
ceremony cost and a large flat list also raises tool-selection errors.

### Options

| Option | Verdict |
| --- | --- |
| **A. One mega-tool** `todo(action, …)` (today's Pi adapter) | Rejected as the only tool: large weakly-validated union; every arg optional so the model guesses; poor discovery. |
| **B. One tool per verb** (~45) | Rejected: context bloat, selection errors. |
| **C. Profiled server, grouped tools** | **Chosen.** |

### Chosen surface

**Profiles:**

- `--profile agent` (default): `tools_work` + `tools_query`.
- `--profile full`: adds `tools_plan`, `tools_findings`, `tools_admin`.

Read-only queries are cheap and constantly wanted, so they are in the default.
`create_item`/`update_item` are heavy schemas and stay out of it.

**`tools_work` — always loaded (6):**

| Tool | Wraps | Notes |
| --- | --- | --- |
| `next` | `AgentWorkflow.next` | First call. Returns claim-or-ready + `next_action`. |
| `take` | `AgentWorkflow.take` | Atomic claim/adopt. **Must pass the server session id** so a restarted server auto-adopts via `_adopt_internal` (§S4). |
| `context` | `AgentWorkflow.context` | Paging (`section`/`cursor`/`limit`), `fields` projection, **and the only way to re-read `claim_token` + `next_action` after a client restart within a claim** (§M9). Stays hot-path. |
| `progress` | `AgentWorkflow.progress` | Marks a unit done, refreshes lease. |
| `finish` | `AgentWorkflow.finish(model_assert=True)` | No-shell gate only (§7). |
| `release` | `AgentWorkflow.release` | Explicit hand-back. |

**`tools_query` — default profile, read-only:** `list_items`, `show_item`,
`ready`, `stats`, `deps`, `deferrals`, `export`, **`check_scope`** (inspect
changed files vs scope — read-only), **`verify_list`** (list stored
verifications, never `--run`), **`lint`** (planning-quality check, read-only),
**`start_unit`** (no-shell state change — marks a unit in progress).
`list_items` / `ready` / `show_item` carry `fields` + `limit`; the byte cap uses
the **drop-trailing-items paging fallback** the CLI already implements, not only
`E_OUTPUT_TRUNCATED` (§S3, §11). `stats` needs an error code for the
unresolved-identity drafts-dir path (§S3).

**`tools_plan` — full profile:** `create_item`, `update_item`, `add_dependency`,
`defer`, `promote_deferral`, `dismiss_deferral`, `block`, `unblock`, `drop`.
(`update_item` is the amend surface; ADR 0006 states whether it is now the
"JSON patch" surface §2.10 declined — §17.)

**`tools_findings` — full profile:** `finding_create` (writes a **draft file
only**, never the DB — preserves the credential-free capture boundary),
`finding_list`, `finding_show`, `finding_triage`, `finding_link`,
`finding_promote`, `finding_dismiss`. **`finding_sync` is NOT a tool** — it is
"the credentialed landing step" and goes to the CLI floor (§9, §S3).

**`tools_admin` — full profile, read/bootstrap only:** `init_project`,
`config_get`. **`config_set` is NOT a tool** (a model could lower
`lint.require_scope_rules` — its own finish gate). **`sweep_stale` is NOT a
tool** (it clears *other principals'* claims — §S3). **`migrate` is NOT a tool**
(§8). All three go to the floor.

**Never a tool, any profile:** verification execution, `rebaseline`, `complete`
(human ladder — see §7 for its fate), `restore` / `restore-legacy`,
`import-yaml`, `finding sync`, `config set`, `sweep-stale`, `migrate`, forensic
`audit` beyond `verify`.

**Rejected:** dynamic tool-list filtering / tags (not portable across targets
today).

---

## 6. Git scope and workspace fingerprint

`GitScopeEngine` (`agent.py:32`) finds the repo root from `Path.cwd()`.

**Decision.** The server builds `GitScopeEngine(repo_root=<pinned §4>)` at
startup and injects it into `AgentWorkflow` via a new `git_engine` parameter
(`AgentWorkflow.__init__` currently takes only `repo_root` — add the seam,
§M9). `progress` / `finish` compute `changed_files` / `workspace_fingerprint`
against the pinned root. If `--repo-root` is not a git repo, mutation tools
return `E_SCOPE_GATE` rather than silently scoping to `cwd`.

`workspace_fingerprint()` reads every untracked file's bytes — it must run in
the worker thread, never on the event loop (§8/B3).

---

## 7. The verification / rebaseline boundary

ADR 0003 §2.4 must survive. Today: `finish --model-assert` is no-shell;
`finish --run-verifications` previews + runs stored shell commands **and
completes the item** (`agent.py:663-704`).

**Decisions:**

- The `finish` **tool** is model-assert only: requires a current
  `workspace_fingerprint` attestation; a stale pass → `E_VERIFY_GATE` with
  `recovery` naming the human command **and the actor + claim token to use**
  (§B5).
- **`todo-db verify-run <id> --claim-token <t> --actor <principal>`** is a floor
  CLI verb. **It attests only** — wire it to `tracker.attest_verifications`
  after previewing + running the ladder + re-checking scope. It does **not**
  complete the item; the model's `finish` tool remains the closer, so the
  model's turn continues after a human attests (§S5). This is a deliberate
  behaviour change from `--run-verifications`.
- `--actor` is required because the claim is held by the server's principal, not
  the human's shell (`default_actor()` would mismatch — §B5).
- `run_verification` refuses hosted DBs unless `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1`;
  that variable is kept **out of the server's environment entirely** (§S5).
- `todo-db rebaseline <id> --reason … --claim-token <t> --actor <principal>` —
  floor CLI, clean-worktree only, `--actor` required.

**Why cleaner than the skill:** the model's vocabulary is the registered tool
set; `verify-run` / `rebaseline` are outside it, so they are not *normalised* or
*accidentally* invocable. The env allowlist and byte cap the Pi adapter
re-implemented in TS now live once, server-side. (Accurate framing: a client's
shell tool can still run them — this removes normalisation, not the shell.)

**Future:** MCP "elicitation" (server asks the human to confirm mid-call) could
make a gated `run_verifications` tool viable when portably supported. Follow-up,
not built here.

---

## 8. Connection model, identity, concurrency (rewritten — three review blockers)

### 8.1 Connection & credentials (§B1, §B2)

- **Startup:** open **READ_ONLY** to run `_check_schema()` + `_check_identity()`
  only. This never migrates and gives a local `file:…?mode=ro` open. If the DB
  is behind the packaged schema → every tool returns `E_SCHEMA` with recovery
  "`todo-db migrate`". If ahead → `E_SCHEMA` "package is stale".
- **Per tool call:** map the tool to a `CredentialMode` (reuse `_mode_for`'s
  table directly), `resolve_credential` for that capability, open a connection,
  do the work, close it. Read-only tools never hold a read-write token. This
  preserves ADR 0005 G1 ("no ambient read-write authority") — the amendment ADR
  0006 records is only that the *server process* is now the credential-scoping
  boundary instead of a child `todo-db` process.
- **Never auto-migrate on a write path.** Add `TodoDatabase.open(..., migrate=False)`
  (or a dedicated read-write-no-migrate classmethod) so write tools open without
  triggering `_migrate()`. `migrate` stays a floor CLI verb + is the only thing
  that runs `_migrate()`.
- **Audit-head check:** `open()` runs `_check_audit_head()` (or full
  `verify_audit()` under `TODO_DB_AUDIT_OPEN_POLICY=full`). With a
  connection-per-call model this runs on **every** call — keeps ADR 0003 §2.4
  finish-gate #4 honest, at a small cost. Measure it against the query budget
  harness.
- **`_PROVIDER_CACHE`:** call `reset_credential_provider_cache()` on
  `E_AUTH_REJECTED` and re-resolve, since the "retry in a fresh process"
  remediation is not something the model can do. Document a supervised-restart
  fallback.

### 8.2 Threading (§B3 — FastMCP does NOT serialize tool calls)

**Contract:** all DB and git work runs on **one dedicated worker thread** owned
by the server. Tools are `async def` and dispatch to it via a single-worker
executor. This means:
- `sqlite3` objects are only ever touched on that one thread (default
  `check_same_thread=True` is fine).
- `GitScopeEngine` subprocesses and `workspace_fingerprint()` byte-reads never
  block the event loop (stdio framing, pings, cancellation stay responsive).
- A `threading.Lock` (or the single-worker executor itself) serialises
  mutations within the process. Cross-process safety is unchanged
  (`BEGIN IMMEDIATE` + `busy_timeout=5000`).

Replaces the Pi `SerializedQueue`.

### 8.3 Identity (§B5)

| Concern | Design |
| --- | --- |
| Principal (`claimed_by`) | `--actor` / `TODO_DB_ACTOR` launch value. **If unset, derive `mcp:<clientInfo.name>:<user>@<host>` from `initialize` — unconditionally. The server never reaches `default_actor()`** (which wrongly treats a session id as a principal, breaking ADR 0003 §2.1/§2.2). |
| Session (`claimed_session`) | UUID per server process; `--session` override. Logged at startup. Passed on every `take` so restart → auto-adopt (§S4). |
| Claim token | Unchanged; required on `progress`/`finish`/`release`. |
| One live claim | Enforced in `current_claim`; cleaner now that the principal is fixed. **But `E_MULTIPLE_CLAIMS` hard-fails `next` AND `take`** (`agent.py:225`) — deadlock. Fix: the `E_MULTIPLE_CLAIMS` envelope must carry every offending item id **and its claim token** in `recovery`, and `claims` stays in the default profile (§S4). |
| Auth (stdio) | Trusted local process — inherits the CLI trust model. Documented. The client-side registration-approval step replaces the Pi `isProjectTrusted()` gate (§M9). |
| `TODO_DB_AUTH_CONTRACT=v2` | The server sets `v2` in its own environment (per-call exit codes are vacuous for it); the **floor CLI keeps the two-way exit-4-vs-2 behaviour** `diff_exit_codes` requires (§M3). `doctor`'s auth-contract WARN check is updated once the wrapper is gone. |

### 8.4 Subagents / forks

A subagent or session fork that starts its own server inherits the same
`--actor` → same principal → same-principal adoption (ADR 0003 §2.2). A fork
that keeps the parent's server connection is fine (one worker thread). Document
that concurrent *distinct* principals on one project still race cooperatively
via claim tokens, as today.

---

## 9. The human / CI floor

Three consumers cannot be served by a model-driven tool call: bootstrap
(`init`/`migrate` before a server runs), CI / release gates, and human recovery.

**Surviving `todo-db` CLI verbs — the floor:**

```
todo-db init | init-project | migrate | doctor
todo-db audit verify | export | restore | restore-legacy | import-yaml
todo-db finding sync                                   # the credentialed landing step
todo-db config get | config set
todo-db sweep-stale
todo-db verify-run <id> --claim-token <t> --actor <p>  # attests only (§7); was `agent finish --run-verifications`
todo-db rebaseline <id> --reason … --claim-token <t> --actor <p>
todo-db complete <id> …                                # kept: human ladder closer for non-agent use / recovery
todo-db mcp …                                          # alias for todo-db-mcp, discoverability
```

**Deleted from the CLI (0.6.0):** the entire `agent` group; `create`, `update`,
`list`, `show`, `ready`, `stats`, `deps`, `start`, `done`, `defer`, `promote`,
`dismiss`, `block`, `unblock`, `release`, `claim`, `check-scope`, `verify`,
`lint`; the `finding` group **except `sync`**. `_run_agent`,
`_agent_instructions`, and the `agent`/`finding` argparse trees leave `cli.py`.

**Reconciliation of review §B4/§S3:** `check-scope`, `verify` (list),
`lint`, `start` become **tools** (§5 `tools_query`), not floor verbs — they are
read-only or no-shell. `complete` **stays a floor verb** (recovery + non-agent
use). `restore-legacy` is in the floor list above and removed from any "deleted"
enumeration. `references/implement.md` is rewritten against this exact split as
a named work unit (§13), not hand-waved.

**Config migration:** existing `.todo-db/config.json` files carry
`"wrapper": "_project/scripts/todo"`. Remove `wrapper`-key handling from
`doctor` (`_doctor_wrapper_check`, `cli.py:1109`) and `init-project`; ship a
one-line migration note ("delete the `wrapper` key"); `doctor` ignores an
unknown key rather than `FAIL`.

**Dependency:** the floor CLI keeps **zero new runtime dependencies**. `mcp` is
`todo-db[mcp]` only (§S8). Confirm the SDK's Python floor; if `>=3.11`,
`requires-python` and the CI matrix move and that is called out in the ADR.

---

## 10. Cross-agent enablement and skill rewrite

### Registration

Ship `docs/operations/mcp-clients.md` with a **per-client** copy-paste block
(not one template — §S2). Claude Code omits `--repo-root`; others set it where
their config format supports project scope; all set `--actor`. Codex's
user-global limitation is called out inline.

### skill-sync

**skill-sync has no generic file renderer** — it has `settings generate` /
`agent-config capture|validate|restore` over a fixed six-file snapshot (§S2).
Emitting MCP registration files is a **feature request against `skill-sync.git`**
(a repo this plan does not own) and sits on the critical path. Until it lands,
`mcp-clients.md` is copy-paste and a work unit tracks the skill-sync feature
separately.

### `todo` skill rewrite (cross-repo catalog release — §S1)

The `todo` skill is `install_mode: mirror`, `tracked: true`, sourced from the
pinned `skill-sync-skills.git` catalog. The rewrite is: **publish to the catalog
repo → bump the pin in `skill-sync.yaml` → `skill-sync` re-sync → commit
`skill-sync.lock` + all three target trees in one commit → CI `skill-sync
verify`.** This is a cross-repo release on the 0.6.0 critical path.

Content: drop the "use `_project/scripts/todo`" contract and the "run `--help`,
treat as contract" instruction; new contract "call the `todo_*` MCP tools;
enable `--profile full` for grooming". Skill-only actions (`ideate`, `spec`,
`prioritize`, `batch`, `handoff`, `closeout`) keep their intent, composing tool
calls. `references/*.md` gain tool-call examples and lose CLI lines — with
`implement.md` rewritten against the §9 split specifically.

### Instructions

`cli.py:1437` text → **`todo://instructions` resource + a `get_instructions`
tool** (portable path). `todo/workflow` MCP **prompt** is additive — Claude Code
surfaces prompts as slash commands, several listed clients ignore them (§S2).

---

## 11. Response envelope, structured output, token budget

**Envelope:**

```json
{ "ok": true,  "data": { … } }
{ "ok": false, "code": "E_CLAIM_STALE", "error": "…", "recovery": ["…"], "kind": "gate" | "error" }
```

- `kind` carries forward the Pi adapter's `isError: !gate` distinction (§M2):
  `gate` = an expected in-band gate result the model should act on; `error` =
  protocol/environment failure.
- Compact JSON (`separators=(",",":")`, `sort_keys=True`).
- `structuredContent` mirrors `data`. **`outputSchema` only on the small tools**
  — declaring it on `context` (eight sections + `completeness`) roughly doubles
  its injected surface (§M9). Text content block stays authoritative for older
  clients.
- Byte cap: default 16 KB, **chosen together with `context`'s default `limit`**
  because `context` at `limit 20` on a large item already exceeds 16 KB (§M9).
  Overflow on list-shaped tools → drop-trailing paging fallback first, then
  `E_OUTPUT_TRUNCATED` with a `section`+`cursor` hint.

**Error taxonomy (§M2 — new, mandatory):** a table mapping every raised
exception to an `E_` code. Today uncoded and would fall through to generic:
`ProjectIdentityMismatchError`, `SchemaBehindError`, `SchemaDivergedError`,
`AuditIntegrityError`, `sqlite3.OperationalError` from `_assert_writable`, the
empty-queue `take` (wire the existing `E_NOTHING_READY`), the drafts-dir raise
in `stats`. Add codes for the uncoded classes in `errors.py` (BenchBox's
`diff_exit_codes` also needs to know — §B6).

**`next_action` becomes machine-readable — and dual-emitted in 0.5.0 (§S7):**

```json
"next_action": {
  "action": "progress", "tool": "progress",
  "arguments": { "id": "x", "wid": "w1", "evidence": "<fill in>", "claim_token": "…" },
  "command": "todo agent progress x w1 --evidence '<evidence>'"
}
```

Keep `command` through 0.5.0 (the Pi adapter and skill still read it); **drop
`command` in 0.6.0**. Every embedded command string in `agent.py` and
`tracker.py` must be audited, not just `next_action.command`:
`agent.py:227, 265, 283, 326, 501, 507, 516, 528`, `tracker.py:1636`, and the
`human_action_required` variants naming `todo unblock` / `todo dismiss`
(`agent.py:490, 496, 523`). In 0.6.0 those name the surviving tool or floor verb.

**`E_LINT_GATE` releases the claim (§S6).** `AgentWorkflow.finish` calls
`self.release()` **before** raising `E_LINT_GATE` (`agent.py:636`). This
contradicts ADR 0003 §2.5 (lint = code failure → claim retained). **ADR 0006
resolves it:** either fix `finish` to retain the claim on lint failure (matches
the ADR, better for the model), or record the divergence. Recommendation: fix
it. Until fixed, the `E_LINT_GATE` envelope's `recovery` must say "the claim was
released; `take` again" and the model must discard its cached token.

**Budget:** hot path ≈ 6 schemas, ~1.3–1.8 KB **without** `outputSchema` on the
big tools. A `next → take → progress → finish` cycle: ~4 structured pairs,
compact JSON, no `--help` probe, no wrapper output, no prose parsing. Net
reduction concentrated in eliminated `--help` reads and the removed wrapper
layer. The `outputSchema` decision (§M9) is what keeps this budget real.

**Schema freeze:** snapshot every tool's name + description + input schema (+
output schema where declared) to `scripts/mcp_snapshots/tools.json`; a test
fails on unplanned drift. The CLI-help / exit-code / DDL freezes stay intact for
the floor (§B6).

---

## 12. Transport: stdio now; HTTP + hosted later

stdio only. Matches the local trust model, needs no auth layer, universally
supported.

HTTP/SSE + hosted-backend support are one additive follow-up: reuse
`TODO_DB_AUTH_TOKEN` as the bearer credential, localhost-bind by default,
explicit `--listen` / `--allow-hosted`, revisited after ADR 0003 §2.9's harness
certifies commit-outcome fault behaviour. A day-long stdio server against a
hosted primary (`HostedConnection` has no reconnect/keepalive; a mid-session 401
→ `E_AUTH_REJECTED` "fresh process") is materially riskier than 45 one-shot
processes — hence the deferral (§M4).

---

## 13. Sequenced delivery — split into 0.5.0 (additive) and 0.6.0 (destructive)

The single-release plan is rejected (§M7): if muse/grok (§15) resolve badly
*after* the adapter is deleted, the only way back is a revert release.

### 0.5.0 — additive; CLI intact; adapter deprecated-but-present

- **ADR 0006** ("MCP is the sole *agent* interface"), citing this plan; amends
  ADR 0005 G1 (§8.1); resolves ADR 0003 §2.5 vs code (§11); supersedes §2.10
  rationale (§17). **Approval gate before code.**
- `todo-db[mcp]` optional dep + `todo-db-mcp` entry point.
- `src/todo_db/mcp/` per §3: connection-per-call (§8.1), single worker thread
  (§8.2), mandatory actor (§8.3), error taxonomy (§M2), envelope + paging
  fallback (§11).
- `AgentWorkflow` gets the `git_engine` seam (§6/§M9); `TodoDatabase` gets the
  no-migrate open path (§8.1).
- `next_action` **dual-emits** `command` + `{tool, arguments}` on **both**
  surfaces (§S7) — coexistence is now真.
- Fix `E_LINT_GATE` claim-retention (§S6) or record the divergence.
- Tools: `tools_work` + `tools_query` (default), `tools_plan` / `tools_findings`
  / `tools_admin` (`--profile full`).
- `todo://instructions` resource + `get_instructions` tool; `todo/workflow`
  prompt (additive).
- Add `E_` codes for the uncoded exception classes; tell BenchBox (§B6).
- Tests §14 including the **subprocess stdio smoke test** and **stdout-purity
  assertion** (§M1, §M6).
- `docs/operations/mcp-clients.md` (copy-paste, per-client).
- Deprecation notices: Pi adapter README, `agent instructions` output, the
  `todo` skill (still functional).
- CHANGELOG: "Added — MCP server (experimental, `todo-db[mcp]`). The `agent` CLI
  and Pi adapter are deprecated and will be removed in 0.6.0."

### Between releases — prove adoption (blocks 0.6.0)

- **Verify every named target** (Claude, Codex, Pi, Gemini, **muse**, **grok**)
  drives the stdio server end-to-end (`next → take → progress → finish`).
- **BenchBox coordination (§B6):** lockstep migration of
  `todo_db_standalone_compat.py` `COMMANDS` / `STANDALONE_ONLY_COMMANDS` /
  `parity_allowlist.json`, or expand the allowlist ahead of the deletions.
- **skill-sync catalog release (§S1):** publish the rewritten `todo` skill,
  bump the pin, re-sync.
- File the **skill-sync MCP-renderer feature request** (§S2).

### 0.6.0 — destructive; MCP is the sole agent interface

- Delete `integrations/pi/` entirely; unpublish `@todo-db/pi-adapter`.
- Remove the `agent` group, `_run_agent`, `_agent_instructions`, `AGENT_*`
  constants, the `finding` group **except `sync`**, and the now-MCP-only CLI
  subcommands from `cli.py`.
- Delete `_project/scripts/todo`, `DEFAULT_WRAPPER_RELATIVE`, `refresh-wrapper`,
  `init-project --wrapper`, `TODO_DB_PI_PRINCIPAL`; strip `wrapper`-key handling
  (§9).
- Rename `agent finish --run-verifications` → `verify-run` (attest-only, §7);
  `agent rebaseline` → `rebaseline`; both take `--actor`.
- Drop `next_action.command` (§S7).
- Reshape `.github/workflows/*.yml`, `scripts/parity_conformance.py` (+
  snapshots), `scripts/mcp_snapshots/`.
- **Rewrite `docs/operations/release-gates.md` Gate 2** against a surviving
  floor verb (`todo-db audit verify` / `export` / `doctor`) — the current
  wording names `todo list` / `ready` / `show`, all deleted, and this migration
  *triggers* Gate 1 + Gate 2 (it touches "the environment allowlist any adapter
  passes to `todo-db`"). The gate must **not** depend on the uncertified hosted
  path (§M8).
- `tests/test_cli_agent.py` deleted; assertions ported to the MCP suite.
- CHANGELOG: "BREAKING — the `agent` CLI, the Pi adapter, and the wrapper are
  removed. MCP (`todo-db-mcp`) is the sole agent interface. A minimal `todo-db`
  CLI remains for bootstrap, CI, audit/export, `finding sync`, and human
  verification/rebaseline."

**Versioning (§S8):** 0.5.0 then 0.6.0 is correct under SemVer 0.y — a breaking
change bumps the minor, and `Development Status :: 3 - Alpha` plus the open §15
items rule out 1.0.0.

---

## 14. Testing

- **Unchanged:** `AgentWorkflow` unit tests, `test_tracker.py`, `test_audit.py`,
  `test_concurrency.py`.
- **`tests/test_mcp_server.py`** (SDK in-memory transport): per tool — envelope
  shape, `kind` gate/error, arg-validation rejects, byte cap + paging fallback,
  `next_action.tool`/`arguments`. Profile test: `agent` exposes exactly
  `tools_work` + `tools_query`; `full` adds the rest; verify-run / rebaseline /
  config-set / sweep-stale / finding-sync / migrate absent at every profile.
- **`tests/test_mcp_stdio_smoke.py`** (§M6): spawn `todo-db-mcp` as a
  subprocess, real `initialize`, `tools/list`, call `next`. Asserts subprocess
  launch, env inheritance, cwd/`--repo-root` resolution (§4).
- **stdout-purity test (§M1):** no tool path writes to stdout; a helper asserts
  every reachable `print` is stderr or removed.
- **`E_MULTIPLE_CLAIMS` recovery test (§S4):** two claims for one principal →
  envelope carries both ids + tokens; `next`/`take` recover.
- **MCP concurrency test:** two servers, one DB, one-winner claim race; stale
  `claim_token` on `progress` → `E_CLAIM_STALE`.
- **Threading test (§B3):** a tool doing DB + git work does not raise
  `ProgrammingError` and does not block a concurrent `initialize` ping.
- **Credential-scope test (§B1):** a read-only tool call never resolves a
  read-write credential (assert via a fake provider recording capability).
- **Schema-gate test (§B2):** server against a behind / ahead DB → `E_SCHEMA`,
  no migration side effect.
- **Deleted (0.6.0):** `tests/test_cli_agent.py`, `integrations/pi/tests/*`.
- **Adjusted:** `test_cli_parity.py`, `test_conformance.py`,
  `test_packaging_smoke.py`, `test_query_budget.py` (the per-call audit-head
  check — §8.1).

---

## 15. Risks and open decisions for review

| # | Decision / risk | Recommendation |
| --- | --- | --- |
| 1 | Zero CLI, or the §9 floor? | **Floor.** A daemon cannot bootstrap itself or serve CI without an agent. |
| 2 | Single server + `--profile`, or two entry points? | **Single + `--profile`.** |
| 3 | Tool granularity | **6 hot-path + grouped rest.** |
| 4 | **Do muse and grok speak MCP stdio?** | **Unknown — verify between 0.5.0 and 0.6.0. Load-bearing for the whole plan.** A target that cannot uses the floor CLI and the "one interface" goal is not met for it. |
| 5 | Codex project-scoped MCP config? | **Verify (§4).** If absent, Codex resolves from cwd. |
| 6 | `structuredContent` / `outputSchema` client support | Populate `structuredContent`; `outputSchema` only on small tools; text block authoritative. |
| 7 | Connection-per-call audit-head cost | Measure against `test_query_budget`; acceptable if it stays within budget. |
| 8 | HTTP transport / hosted backend | **Deferred (§12).** |
| 9 | `E_LINT_GATE` claim release contradicts ADR 0003 §2.5 | **Fix `finish` to retain the claim** (§S6); ADR 0006 records the fix. |
| 10 | `verify-run` attest-only vs complete | **Attest-only (§7).** `finish` stays the closer. |
| 11 | Pi status widget | Drop with the adapter; re-add later as a `next`-only Pi extension if missed. |
| 12 | skill-sync MCP renderer is another repo's feature | File the request; `mcp-clients.md` is copy-paste until it lands. |
| 13 | mcp SDK Python floor vs `requires-python = ">=3.10"` | Confirm before pinning; if `>=3.11`, move `requires-python` + CI matrix and call it out in the ADR. |

---

## 16. One-paragraph summary

Build `src/todo_db/mcp/` as a FastMCP **stdio** server that is a thin transport
over the existing `AgentWorkflow`, with a **connection-per-call**,
capability-scoped credential model (ADR 0005 G1 preserved), **all DB and git
work on one dedicated worker thread** (FastMCP does not serialise), and a
**mandatory explicit actor** (never `default_actor()`). Expose a 6-tool hot path
plus read-only queries (including `check_scope`, `verify_list`, `lint`,
`start_unit`) by default, with `--profile full` for planning / findings /
admin — but `finding sync`, `config set`, `sweep-stale`, `migrate`,
verification execution, and `rebaseline` are **floor CLI verbs**, not tools.
Make `next_action` machine-readable (dual-emit `command` in 0.5.0, drop in
0.6.0). Ship **0.5.0 additive** (server + skill rewrite via the catalog, CLI
intact, adapter deprecated) and **0.6.0 destructive** (delete adapter, wrapper,
`agent` group; reshape CLI and release gates) once muse/grok MCP support and
BenchBox parity are proven. Fix the `E_LINT_GATE` claim-release bug against ADR
0003 §2.5 on the way.

---

## 17. ADR 0003 §2.10 handling

§2.10's **decision** — "no separate JSON patch command" — **stands**. Only its
rationale's clause "Retaining existing CLI flags preserves canonical audit
contracts" is obsoleted (the flags are going). ADR 0006 restates the decision,
supersedes the rationale, and states explicitly: `update_item` (with its
~20 add/drop array parameters) **is** the audited amend surface, it is
`--profile full` only, and it is not a general JSON-patch mutation channel —
lifecycle state still moves only through the dedicated verbs.
