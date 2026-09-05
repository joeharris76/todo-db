# Queries and item management

Inspect, filter, and manage tracker items using MCP tools without direct
database queries.

## Query tools

| Goal | Tool call | Notes |
|---|---|---|
| View ready items | `ready(fields=[...], limit=20, cursor=0)` | Returns unblocked items with satisfied dependencies. |
| List items | `list_items(fields=[...], limit=20, cursor=0)` | Supports field projection and paging; filter client-side. |
| Inspect one item | `show_item(id=..., fields=[...])` | Retrieves full item details or selected fields. |
| Check dependencies | `deps(id=...)` | Returns upstream prerequisite items (`needs`). |
| View open deferrals | `deferrals()` | Lists open work parked for later triage. |
| Full item dump (explicit request only) | `export(confirm_full_snapshot=true)` | Returns all items and verification ladders; expensive over hosted databases and does not return audit history. |
| Check held claims | `claims()` | Shows all active claims held by your principal. |
| System statistics | `stats()` | Item counts by priority, state, and findings. |

## Managing output limits

Responses are capped at 16 KiB. To avoid `E_OUTPUT_TRUNCATED`:
- Request only necessary fields:
  `list_items(fields=["id", "title", "priority", "state"])`
- Use pagination:
  Pass `limit=20` and increment `cursor` across pages.
- `export()` is not a normal inspection query and is not an audit-history
  lookup. It produces an uncapped full item dump; use `list_items`, `ready`,
  `show_item`, or `context` during normal sessions. If those tools omit data
  you need, report the unsupported read instead of calling `export()` as a
  probe.

## Administrative mutations (profile: full)

When running with `--profile full`, agents can manage item lifecycle flags:

- **Block an item**:
  `block(id="ITEM-1", reason="Waiting for API access")`
- **Unblock an item**:
  `unblock(id="ITEM-1")`
- **Drop an item**:
  `drop(id="ITEM-1", reason="Superseded by architectural refactor")`
