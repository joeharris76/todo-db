# `todo_db.mcp` — the MCP stdio server

This package is the agent interface for the tracker (ADR 0007). It builds the
server, resolves the state branch target, pins the worker identity, and
registers thirteen task tools over shared service operations.

## Layout

| File | Purpose |
| --- | --- |
| `__init__.py` | Package doc and a `main` shim. |
| `__main__.py` | `python -m todo_db.mcp` → `server.main()`. |
| `server.py` | Launch args, stderr logging, `LaunchConfig`, `build_server`, lifespan, `startup_check`, `main`. |
| `target.py` | State target resolution (flag > env > upward discovery), pinned for the process lifetime. |
| `identity.py` | Worker identity (`--actor` → `TODO_DB_ACTOR` → `mcp:<clientInfo.name>:<user>@<host>`) and the per-process session id. |
| `tools.py` | The thirteen task tools: `list_items`, `show_item`, `create_item`, `register_batch`, `update_item`, `take`, `prepare`, `bind_batch_pr`, `abort_batch`, `release`, `finish`, `renew`, `drop`. |
| `resources.py` | `todo://instructions` resource, `get_instructions` tool, and `todo/workflow` prompt — all the same text. |
| `instructions.py` | The workflow protocol text. |

## Tools

One server instance is one worker identity: concurrent workers run separate
servers with different `--actor` values. Blocking Git work runs via
`asyncio.to_thread`; there is no shared mutable connection to guard.

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

`scripts/mcp_snapshots/tools.json` freezes the registered tool names,
descriptions, and input schemas. `tests/test_mcp_stdio.py` compares the live
server against it, so any tool change must land with a regenerated snapshot.
