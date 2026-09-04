# MCP client registration for `todo-db`

`todo-db` ships one MCP server, `todo-db-mcp`, installed with the `mcp` extra.
One server instance serves one project, which is one worktree. It speaks
**stdio**; there is no HTTP transport.

```
todo-db-mcp [--repo-root <path>] [--profile {agent,full}] [--actor <principal>]
            [--log-level {debug,info,warning,error}] [--allow-hosted]
```

Common flags:

- `--repo-root` — project root for config discovery and git scope. Omit it when
  the client launches the server from the project root; the server defaults to
  its working directory.
- `--profile` — `agent` (default) exposes the workflow tools, the read-only
  queries, and the three planning tools (`create_item`, `update_item`,
  `add_dependency`). `full` adds findings, `block`/`unblock`/`drop`,
  `init_project`, and `config_get`.
- `--actor` — audit principal. **Prefer omitting it.** With no `--actor` the
  server derives `mcp:<clientInfo.name>:<user>@<host>` from the `initialize`
  handshake, resolving the host itself. A hand-written value such as
  `claude:${USER}@${HOSTNAME}` is worse than nothing in most clients, because
  `HOSTNAME` is a shell variable rather than an exported environment variable
  and expands to empty — writing a truncated principal like `claude:joe@` into
  `claimed_by` and every audit row.
- `--allow-hosted` — required to open a hosted (Turso/libSQL) target. The
  server is local-SQLite-first.

Logging goes to **stderr only**; stdout carries JSON-RPC framing.

## Claude Code

File: `.mcp.json` at the **project root** (Claude Code launches servers from
there). Do not set `--repo-root`; the default working directory is already the
project root. Note that `${workspaceFolder}` does **not** expand in Claude
Code — only `${VAR}` and `${VAR:-default}` for environment variables.

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

For the full profile:

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--profile", "full"]
    }
  }
}
```

Claude Code surfaces MCP **prompts** as slash commands (`/todo/workflow`). The
`todo://instructions` resource and the `get_instructions` tool are the portable
fallbacks for clients that do not.

## Codex

File: `~/.codex/config.toml`, which is user-global. A single
`[mcp_servers.todo-db]` entry cannot carry a per-project `--repo-root`, so
Codex resolves the project from its own working directory through the same
upward `.todo-db/config.json` discovery the CLI uses.

```toml
[mcp_servers.todo-db]
command = "todo-db-mcp"
args = []
```

Full profile:

```toml
[mcp_servers.todo-db]
command = "todo-db-mcp"
args = ["--profile", "full"]
```

## Cursor

File: `.cursor/mcp.json`, or Cursor's global MCP settings. Both use an
`mcpServers` block; `${workspaceFolder}` expansion varies by version, so omit
`--repo-root` if it does not expand.

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Windsurf

File: `~/.codeium/windsurf/mcp_config.json`, or the Windsurf UI.

```json
{
  "mcpServers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Zed

File: `~/.config/zed/settings.json`, under `context_servers` (Zed's name for
MCP servers).

```json
{
  "context_servers": {
    "todo-db": {
      "command": "todo-db-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Continue

File: `~/.continue/config.json`. Continue's MCP support is experimental; check
your version's schema.

```json
{
  "mcpServers": [
    {
      "name": "todo-db",
      "command": "todo-db-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  ]
}
```

## Any other stdio MCP client

Nothing above is client-specific beyond the file location and the surrounding
key names. Any client that launches a stdio MCP server works: point it at the
`todo-db-mcp` command with no arguments, from the project root.

A client that does not speak MCP cannot drive the tracker. The floor CLI covers
bootstrap, CI, audit, export, and human recovery, but it deliberately has no
planning or lifecycle verbs — those live only on the MCP surface (ADR 0006).

## Verifying a registration

Confirm the database and identity resolve, using the floor CLI:

```sh
todo-db doctor
```

Then drive the workflow through the client:

1. `next` — inspect the ready queue, or the claim you already hold.
2. `take` — claim an item, or re-adopt your active claim.
3. `context` — re-read `claim_token` and `next_action` (also the recovery path
   after a restart).
4. `progress` — record each work unit with evidence; this refreshes the lease.
5. `finish` — the close gate.

Every `next_action` returned by `next`, `take`, `progress`, and `context` is
machine-readable: `{"tool": "...", "arguments": {...}}`.

## The bundled `todo-db` skill

This repository ships a `todo-db` agent skill that teaches the workflow above,
mirrored into `.claude/skills/`, `.codex/skills/`, and `.gemini/skills/` so it
is available to anyone who clones the repository without installing anything
else. See [`skill-deployment.md`](skill-deployment.md) for how those mirrors
are generated and verified.

## Hosted backends

Hosted (Turso/libSQL) targets are gated behind `--allow-hosted` plus a
credential from `TODO_DB_AUTH_TOKEN` or `TODO_DB_CREDENTIAL_COMMAND`. The
server is local-SQLite-first.

A long-lived stdio server against a hosted primary has no reconnect or
keepalive, so a credential that expires mid-session surfaces as
`E_AUTH_REJECTED` and needs a fresh process. Agent mutations against hosted
Turso remain experimental: real commit-outcome fault behaviour is still
unmeasured (ADR 0003 §2.9). See
[`hosted-credentials.md`](hosted-credentials.md) for provisioning and rotation.
