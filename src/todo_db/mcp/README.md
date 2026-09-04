# `todo_db.mcp` — the MCP stdio server

This package is the agent interface for the tracker (ADR 0006). It builds the
server, resolves the project and database target, pins the audit principal, and
registers the tools over `AgentWorkflow` with connection-per-call credentials
and one dedicated worker thread.

## Layout

| File | Purpose |
| --- | --- |
| `__init__.py` | Package doc and a `main` shim. |
| `__main__.py` | `python -m todo_db.mcp` → `server.main()`. |
| `server.py` | Launch args, stderr logging, `LaunchConfig`, `build_server`, lifespan, `startup_check`, `main`. |
| `target.py` | Project/database target resolution (flag > env > upward discovery), pinned for the process lifetime. |
| `identity.py` | Principal (`--actor` → `TODO_DB_ACTOR` → `mcp:<clientInfo.name>:<user>@<host>`; never `default_actor()`) and the per-process session id. |
| `worker.py` | The single dedicated worker thread. All database and git work goes through it (ADR 0006 G4). |
| `dbpool.py` | Per-tool credential capability and connection lifecycle. |
| `envelope.py` | The `{ok, data}` / `{ok, code, error, recovery, kind}` response envelope and the 16 KiB cap. |
| `resources.py` | `todo://instructions` resource, `get_instructions` tool, and `todo/workflow` prompt — all the same text. |
| `instructions.py` | The workflow protocol text. |
| `tools_work.py` | Hot-path lifecycle tools: `next`, `take`, `context`, `progress`, `finish`, `release`, `claims`. |
| `tools_query.py` | Read-only queries, loaded in every profile. |
| `tools_full.py` | `register_planning_tools` (every profile) plus findings and admin (`--profile full`). |

## Profiles

`--profile agent` (default) registers the work tools, the query tools, and the
planning tools. `--profile full` adds findings, `block`/`unblock`/`drop`,
`init_project`, and `config_get`.

Verification execution and `rebaseline` have **no tool at any profile**. A
human runs them from the floor CLI (ADR 0006 G6).

## SDK pin

`mcp>=1.10.0,<2`. The 2.x major is a breaking rename
(`mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`, and
`mcp.shared.memory` changed) while this package is written against the FastMCP
API. The wire protocol is unchanged across that major, so client interop is
unaffected by staying on 1.x.

`FastMCP` does not forward a version to the lowlevel server, so `server.py`
sets `_mcp_server.version` directly; otherwise `serverInfo` reports the SDK's
version to every client instead of the tracker's.

## Snapshots

`scripts/mcp_snapshots/tools.json` and `tools_full.json` freeze the registered
tool names, descriptions, and input schemas. `tests/test_mcp_stdio.py`
compares the live server against them, so any tool change must land with a
regenerated snapshot.
