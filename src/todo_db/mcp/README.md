# `todo_db.mcp` — MCP stdio server (sole agent interface, ADR 0006)

This package builds the server and the lifecycle tools. The six hot-path
tools (`next`, `take`, `context`, `progress`, `finish`, `release` plus
`claims`), read-only queries, and `--profile full` planning/findings/admin
tools are implemented here (`tools_work.py`, `tools_query.py`,
`tools_full.py`) over `AgentWorkflow` with connection-per-call credentials
and one dedicated worker thread.

## What is here

| File | Purpose |
| --- | --- |
| `__init__.py` | Package doc + `main` shim. |
| `__main__.py` | `python -m todo_db.mcp` → `server.main()`. |
| `server.py` | Launch-arg parsing, stderr logging, `LaunchConfig`, `build_server`, lifespan, `startup_check`, `main`. |
| `target.py` | Project/DB target resolution (flag > env > upward discovery), pinned for process life. |
| `identity.py` | Principal (`--actor` → `TODO_DB_ACTOR` → `mcp:<clientInfo.name>:<user>@<host>`; never `default_actor()`) + per-process session id. |
| `worker.py` | The single dedicated worker thread; `run_in_worker` / `run_in_worker_sync`. All DB and git work must go through it (ADR 0006 G4). |
| `resources.py` | `todo://instructions` resource, `get_instructions` tool, `todo/workflow` prompt — all return the same text. |
| `instructions.py` | The workflow text, copied verbatim from `cli.py` `_agent_instructions` (do not import from `cli.py`). |

## Launch args

`--config`, `--db`, `--repo-root`, `--actor`, `--session`,
`--profile {agent,full}` (default `agent`), `--log-level {debug,info,warning,error}`
(default `info`), `--allow-hosted`.

`--log-level` logs to **stderr only** — stdout is JSON-RPC framing.

## SDK notes

- **Package / version pin.** `mcp` (the official Model Context Protocol Python
  SDK). Latest on PyPI at implementation time is **2.1.1**; `uv.lock` resolves
  this repo to **1.29.1**.
- **Pin chosen: `mcp>=1.9.0,<2`.** `mcp` 2.x is a hard breaking rename —
  `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`, and
  `mcp.shared.memory` changed — while ADR 0006 / the migration plan are written
  entirely against the FastMCP API. The **wire protocol is unchanged** across
  the SDK major, so client interop (Claude Code, Codex, …) is unaffected by
  staying on 1.x. **Escalation for the controller:** decide whether a follow-up
  item ports this package to the `mcp` 2.x `MCPServer` API before `0.6.0`, or
  whether `<2` is the long-term pin.
- **Python floor: not an issue.** Both `mcp` 1.29.1 and 2.1.1 declare
  `requires-python >=3.10`, matching this repo's `requires-python = ">=3.10"`.
  No `requires-python` change and no CI-matrix move is needed. (Plan §S8 / risk
  #13 asked this be confirmed — it is confirmed clear.)

## Deferred to other items

- **`todo-db mcp` CLI alias.** A `mcp` subcommand on `todo-db` (for
  discoverability, per plan §9) is **deferred to the `mcp-cli-floor-and-gates`
  item**. `cli.py` is out of scope for `mcp-server-foundation` and edits to it
  fail the item. Use the `todo-db-mcp` entry point or `python -m todo_db.mcp`.
- Lifecycle tools, the response envelope + error taxonomy, `dbpool` /
  connection-per-call, `next_action` dual-emit — all later items.
