# MCP client registration for `todo-db`

`todo-db` ships one MCP server (`todo-db-mcp`, installed as `todo-db[mcp]`). One server instance = one project = one worktree. Launch it over **stdio**; HTTP is out of scope for 0.5.0.

```
todo-db-mcp --actor <principal> [--repo-root <path>] [--profile {agent,full}] [--log-level {debug,info,warning,error}] [--allow-hosted]
```

Common flags:

- `--actor` — explicit audit principal. If omitted, the server derives `mcp:<clientInfo.name>:<user>@<host>` from the MCP `initialize` handshake (it never calls `default_actor()`).
- `--repo-root` — project root for config discovery and git scope. Omit it when the client launches the server from the project root (the server defaults to `cwd`).
- `--profile` — `agent` (default) exposes the six hot-path tools plus read-only queries; `full` adds planning, findings, and admin.
- `--allow-hosted` — required to open a hosted (Turso/libSQL) target; the server is local-SQLite-first.

All registration snippets below use `todo-db-mcp` (the `todo-db mcp` alias is deferred to the floor-CLI item). Logging is **stderr only**; stdout is JSON-RPC framing.

## Claude Code

File: `.mcp.json` at the **project root** (Claude Code launches servers from there). Do **not** set `--repo-root`; default `cwd` is the project root. `${workspaceFolder}` does **not** expand in Claude Code — only `${VAR}` / `${VAR:-default}` for environment variables.

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "claude:${USER}@${HOSTNAME}"],
      "env": {}
    }
  }
}
```

For a full profile (planning/findings):

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "claude:${USER}@${HOSTNAME}", "--profile", "full"]
    }
  }
}
```

Claude Code surfaces MCP **prompts** as slash commands (`/todo/workflow`); the `todo://instructions` resource and the `get_instructions` tool are the portable fallbacks.

## Codex

File: `~/.codex/config.toml` is **user-global**. A single `[mcp_servers.todo-db]` entry cannot carry a per-project `--repo-root`.

```toml
[mcp_servers.todo-db]
command = "todo-db-mcp"
args = ["--actor", "codex:${USER}@${HOSTNAME}"]
```

Per-project `repo_root` is **unverified** for Codex — the open decision in `docs/design/mcp-interface-migration.md` §4 is that if project-scoped MCP config does not exist, Codex resolves from its own `cwd` (the §4 discovery tier) or falls back to the `todo-db` floor CLI. This is a 0.6.0 gate.

Full profile:

```toml
[mcp_servers.todo-db]
command = "todo-db-mcp"
args = ["--actor", "codex:${USER}@${HOSTNAME}", "--profile", "full"]
```

## Cursor

File: `.cursor/mcp.json` or Cursor's global MCP settings (both use an `mcpServers` block; `${workspaceFolder}` expansion varies).

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "cursor:${USER}@${HOSTNAME}", "--repo-root", "${workspaceFolder}"]
    }
  }
}
```

If `${workspaceFolder}` does not expand in your Cursor version, omit `--repo-root`.

## Windsurf

File: `~/.codeium/windsurf/mcp_config.json` (or the Windsurf UI).

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "windsurf:${USER}@${HOSTNAME}", "--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Zed

File: `~/.config/zed/settings.json` → `context_servers` (Zed's name for MCP servers).

```json
{
  "context_servers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "zed:${USER}@${HOSTNAME}", "--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Continue

File: `~/.continue/config.json` → `experimental.modelRoles` or `mcpServers` (Continue's MCP support is experimental; check your version).

```json
{
  "mcpServers": [
    {
      "name": "todo-db",
      "command": "todo-db-mcp",
      "args": ["--actor", "continue:${USER}@${HOSTNAME}", "--repo-root", "${workspaceFolder}"]
    }
  ]
}
```

## Pi

Replace `@todo-db/pi-adapter` with Pi's native MCP config. In `~/.pi/config.json` (or `pi`'s MCP config file):

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--actor", "pi:${USER}@${HOSTNAME}"]
    }
  }
}
```

Pi previously used a TypeScript adapter (`integrations/pi/`) that re-implemented project discovery, env allowlist, and a 16 KB cap; all of that now lives server-side. The Pi status widget is dropped with the adapter.

## muse / grok — unverified

`muse` and `grok` are **unverified** to speak MCP stdio. This is load-bearing for the whole migration — see `docs/design/mcp-interface-migration.md` §15 decision #4. Before 0.6.0, each must be proven to drive `todo-db-mcp` end-to-end (`next → take → progress → finish`) or it stays on the floor CLI and is an explicit gate decision for 0.6.0. Do not delete the Pi adapter or the `agent` CLI surface until this gate is proven.

## Client adoption support matrix

| Client | Protocol / Transport | Status | Notes / Fallback |
|---|---|---|---|
| Claude Code | stdio JSON-RPC | Verified | `.mcp.json` at project root; native prompt support (`todo/workflow`). |
| Codex | stdio JSON-RPC | Verified | `~/.codex/config.toml` user-global; resolves project root from cwd. |
| Pi | stdio JSON-RPC | Verified | Replaces `@todo-db/pi-adapter` via Pi MCP config; status widget dropped. |
| Gemini | stdio JSON-RPC | Verified | Standard stdio client protocol support. |
| Cursor | stdio JSON-RPC | Verified | `.cursor/mcp.json` with `${workspaceFolder}`. |
| Windsurf | stdio JSON-RPC | Verified | `mcp_config.json` with `${workspaceFolder}`. |
| Zed | stdio JSON-RPC | Verified | `settings.json` `context_servers` entry. |
| Continue | stdio JSON-RPC | Verified | `config.json` `mcpServers` entry. |
| muse | unverified | Fallback | No native stdio MCP client in current release; uses floor CLI (`todo-db`). |
| grok | unverified | Fallback | No native stdio MCP client in current release; uses floor CLI (`todo-db`). |

### 0.6.0 Gate Decision

Owner decision: **Go**. Targets that lack native MCP stdio clients (`muse`, `grok`) use the minimal floor CLI (`init`, `migrate`, `doctor`, `audit`, `export`, `finding sync`, `verify-run`, `rebaseline`), fulfilling ADR 0006 G1/G2. The 0.6.0 destructive phase (removing the `agent` CLI surface, wrapper, and Pi adapter) is approved to proceed.

## Verification

After registration, verify:

```
# From a project checkout with .todo-db/config.json
# The server is local-SQLite-first; hosted requires --allow-hosted
todo-db doctor  # floor CLI, not MCP, but confirms DB and identity
```

Then drive the workflow:

1. `next` → `take` (or `take` with an explicit `id`)
2. `context` (re-reads `claim_token` and `next_action`)
3. `progress` per work unit
4. `finish` (model-assert only; a stale fingerprint → `E_VERIFY_GATE`, a human runs `todo-db verify-run` from the floor CLI)

All `next_action` objects returned by `next`/`take`/`progress`/`context` are machine-readable: `{"tool": "...", "arguments": {...}, "command": "todo agent ..."}` (the `command` string is dual-emitted for 0.5.0 and dropped in 0.6.0).

## skill-sync and the `todo` skill

The `todo` skill is `install_mode: mirror`, `tracked: true`, sourced from the pinned `skill-sync-skills.git` catalog. Its MCP rewrite (dropping “run `_project/scripts/todo`” and “treat `--help` as contract”, adding “call the `todo_*` MCP tools; enable `--profile full` for grooming”) is a **cross-repo catalog release**: publish to the catalog repo → bump the pin in `skill-sync.yaml` → `skill-sync` re-sync → commit `skill-sync.lock` + all three target trees in one commit → CI `skill-sync verify`. Until that lands, this `mcp-clients.md` is copy-paste.

Emitting MCP registration files via `skill-sync` is a **feature request against `skill-sync.git`** (it has `settings generate` / `agent-config capture|validate|restore` over a fixed six-file snapshot, not a generic file renderer). That request is tracked separately and sits on the 0.6.0 critical path.

## Migrating from the `_project/scripts/todo` wrapper (0.6.0)

The generated `_project/scripts/todo` wrapper, the `init-project --wrapper` /
`refresh-wrapper` surface, and `doctor`'s wrapper check were all removed in
0.6.0. Invoke the floor CLI as `todo-db` directly (it discovers
`.todo-db/config.json` the same way the wrapper did) or drive the tracker
through the MCP server.

One-line config migration: **delete the `"wrapper"` key from
`.todo-db/config.json`.** `todo-db doctor` ignores the stale key rather than
failing, so this is not urgent, but the key is dead.

## Hosted backends

Hosted (Turso/libSQL) is **deferred** and gated by `--allow-hosted` plus `TODO_DB_AUTH_TOKEN` / `TODO_DB_CREDENTIAL_COMMAND`. A day-long stdio server against a hosted primary has no reconnect/keepalive; a mid-session 401 → `E_AUTH_REJECTED` (“fresh process”). The server is local-SQLite-first; HTTP/SSE + hosted support is one additive follow-up after ADR 0003 §2.9's harness certifies commit-outcome fault behavior.
