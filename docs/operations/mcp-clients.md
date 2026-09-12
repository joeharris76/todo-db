# MCP client registration for `todo-db`

`todo-db` ships one MCP server, `todo-db-mcp`, installed with the `mcp` extra.
One server instance is one worker identity against one state branch. It speaks
**stdio**; there is no HTTP transport.

```
todo-db-mcp [--repo-root <path>] [--config <path>]
            [--state-remote <url-or-path>] [--state-branch <name>]
            [--cache-dir <path>] [--actor <worker>]
            [--session <id>] [--log-level {debug,info,warning,error}]
```

Common flags:

- `--repo-root` — project root for `.todo-db/config.json` discovery. Omit it
  when the client launches the server from the project root.
- `--state-remote` / `--state-branch` — the authoritative state branch.
  Flags beat `TODO_DB_STATE_REMOTE` / `TODO_DB_STATE_BRANCH`, which beat the
  discovered config.
- `--actor` — stable worker identity. It overrides `TODO_DB_ACTOR`; either form
  supports ordinary restart/re-adoption. Give concurrent logical workers
  different actors, and never run one actor in two servers at the same time.
- With no actor, the server derives an instance-scoped identity from
  `clientInfo.name`, user/host, and its per-process session. Separate default
  servers are isolated even when their client names match.
- `--session` — override the generated session identifier. Reusing the same
  value with the same client name and user/host deliberately recreates a
  fallback identity for restart recovery. Do not put one shared value in a
  global registration: concurrent servers would then be the same worker.
- `--cache-dir` — local snapshot cache (default `~/.cache/todo-db-state`).

Logging goes to **stderr only**; stdout carries JSON-RPC framing.

### Upgrade and restart recovery

Claims made by 0.7.3 or earlier use the legacy fallback
`mcp:<clientInfo.name>:<user>@<host>`. An upgraded default server will not
silently adopt one of those claims. Stop the old server first, then either:

1. restart with `--actor '<exact legacy claim worker value>'` and call `take` on
   the same item, which rotates the generation; or
2. let the lease expire and take the item with the new server.

For new claims, prefer a stable unique `--actor` when automatic recovery is
required. A fallback worker can instead record the startup `session id` from
stderr and restart once with `--session '<recorded id>'`. These identities and
claim generations are cooperative credentials, not authentication secrets.

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
bootstrap, validation, migration, and human recovery, but it deliberately has no
planning or lifecycle verbs — those live only on the MCP surface (ADR 0007).

## Verifying a registration

Confirm the state branch resolves, using the floor CLI:

```sh
todo-db validate
```

Then drive the workflow through the client:

1. `list_items(ready_only=true)` — find claimable work.
2. `take` — claim a task; keep the returned `generation`.
3. `show_item` — read needs, readiness, and sections.
4. `renew` — extend a long-running claim.
5. `finish` — close the task.

## The `todo` skill

The canonical `todo` skill in `skill-sync-skills` teaches the workflow above,
mirrored into `.claude/skills/`, `.codex/skills/`, and `.gemini/skills/`. See
[`skill-deployment.md`](skill-deployment.md) for how those mirrors are
generated and verified.

## State remotes

The state branch shares its repository's access and visibility. Never publish
credentials to it and never assume a separate branch is private. A long-lived
stdio server holds no connection: every operation fetches the accepted tip, so
an unreachable remote surfaces as `E_OFFLINE` and mutations fail rather than
succeeding locally.
