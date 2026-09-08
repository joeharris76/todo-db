# Measurements

How the 0.7.0 budgets were measured, on what, and what they do not prove.
Regenerate with `uv run python scripts/measure_state.py [--scale N ...]`.
All fixtures are disposable local bare remotes on this machine
(macOS, local disk); hosted latency was not measured.

## Method

- Tokenizer: `o200k_base` via tiktoken (`TrackerService.count_tokens`;
  `chars/4` fallback only when tiktoken is absent — it was present here).
- Counted surfaces are actual serialized payloads: the committed
  `scripts/mcp_snapshots/tools.json` plus `INSTRUCTIONS` for startup, and
  `json.dumps` of real tool results for list/show/take/finish.
- Seeding writes one commit per scale (`scripts/measure_state.py seed`);
  publication latency is measured separately on sampled single-operation
  publishes (create, take, finish). Seeding is not the publication path.
- Scales: 100 and 10,000 items.

## Token budgets (measured 2026-09-08)

| Surface | Measured | Target |
| --- | --- | --- |
| Startup: 9 tool schemas + instructions | 2,950 tokens | — (was 3,344 for 26 tool defs alone, 4,688 for 38) |
| Five list rows | 288–297 tokens | ~300 |
| One take with work context | 128–133 tokens (small fixture) | ~1,500 for a full task context |
| One finish acknowledgement | ≤ 100 tokens (asserted in `test_mcp_slim.py`) | ~100 |

Targets are benchmark acceptance targets, not universal guarantees: token
counts depend on task content, and multibyte text is counted as encoded.
Every response additionally honors a hard 16 KiB byte ceiling on its final
serialization. A 60 KB description stays bounded: `show_item` spills it to
`field`/`offset`/`budget` section reads with guaranteed progress
(`tests/test_service.py`, `tests/test_mcp_slim.py`).

## Latency and Git growth (local disk)

| Metric | 100 items | 10,000 items |
| --- | --- | --- |
| Seed (one commit) | 0.23 s | 48.9 s |
| 5-row list page | 0.14 s | 3.9 s |
| One show | 0.11 s | 1.1 s |
| Single publish (create/take/finish) | ~0.28 s | ~2.9 s |
| Bare-remote growth per item | ~45 B | ~47 B |

Each operation currently clones the state remote into a private workspace,
so clone cost dominates at 10,000 items (~3 s locally). That is the known
scaling cost of per-writer isolation; a persistent worktree plus fetch
would reduce it, at the price of shared scratch state. Not implemented:
at ordinary tracker sizes the cost is sub-second.

## Footprint

| Tree | 0.6.x baseline | 0.7.0 |
| --- | --- | --- |
| `src/todo_db` Python | 8,563 lines + 237 migration lines | 3,101 lines, no migrations |
| `tests/` | 7,870 lines | 1,134 lines |
| MCP tools | 26 default / 38 full | 8 + `get_instructions` |
| Runtime dependencies | libsql, cryptography, pyyaml (extras) | none beyond the `mcp` extra |
| Dev dependencies | + libsql, cryptography, pyyaml | + tiktoken (budget measurement) |

Test lines fell because the removed suite tested the removed product
(hosted backends, audit chain, findings, workflow gates, SQL budgets).
Retained behaviors — readiness, priority ordering, cycle rejection,
one-winner claims, bounded output, migration mapping — are ported into
the new suite, not deleted. 46 tests pass: `uv run pytest -q`.
