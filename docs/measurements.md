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
| Startup: 10 tool schemas + instructions | 3,150 tokens | — (was 3,344 for 26 tool defs alone, 4,688 for 38) |
| Five list rows | 288–316 tokens | ~300 |
| One take with work context | 128–137 tokens (small fixture) | ~1,500 for a full task context |
| One finish acknowledgement | ≤ 100 tokens (asserted in `test_mcp_slim.py`) | ~100 |

Targets are benchmark acceptance targets, not universal guarantees: token
counts depend on task content, and multibyte text is counted as encoded.
Every response additionally honors a hard 16 KiB byte ceiling on its final
serialization. A 60 KB description stays bounded: `show_item` spills it to
`field`/`offset`/`budget` section reads with guaranteed progress
(`tests/test_service.py`, `tests/test_mcp_slim.py`).

## Latency and Git growth (local disk)

Measured 2026-10-17 with the object-store cache
([ADR 0011](adr/0011-git-object-store-cache.md)), alongside the same script
run on the previous release (0.9.1) on the same machine minutes apart.

| Metric | 100 items | 10,000 items | 0.9.1, 100 items | 0.9.1, 10,000 items |
| --- | --- | --- | --- | --- |
| Seed (one commit) | 0.21 s | 71.6 s | 0.23 s | 67.2 s |
| 5-row list page (first read) | 0.13 s | 0.21 s | 0.14–0.17 s | 2.54 s |
| One show | 0.04 s | 0.10 s | 0.02 s | 0.41 s |
| Single publish (create/take/finish) | ~0.20 s | ~0.41 s | ~0.27 s | ~3.3 s |
| Bare-remote growth per item | ~45 B | ~47 B | ~45 B | ~47 B |

Each read and write fetches only the objects its namespace's store
lacks, so the per-operation cost no longer grows with the branch's
history. At 100 items a show costs about 20 ms more than 0.9.1: three
local git commands replace reading a few files, which is within the
0.11 s recorded for 0.7.0. Seeding is one large commit and is dominated
by validation and hashing, not the cache.

## Local cache size

Measured 2026-10-17 against BenchBox production state (3,383 items,
5,114 commits):

| Measure | 0.9.1 snapshot cache | Object store |
| --- | --- | --- |
| Cost of the current tip | ~30 MiB per revision, kept forever | whole history: 14.7 MiB |
| First read for a namespace | not measured (depth-1 fetch of the tip) | 7.2 s (full history, once) |
| Repeat read at the same tip | 2.97 s to load the cached snapshot alone | 0.69 s (whole `validate` command) |
| Growth per one-item commit, after maintenance | ~30 MiB | 0.58× the bytes of the item touched (20 commits, full repack) |
| Unpacked slack before maintenance | — | ~180 KiB per commit; capped at 8 MiB |

The test suite asserts the bound (`test_size_per_interaction_is_within_twice_the_items_touched`:
0.21× on its 300-item fixture). Before maintenance, a fetched tip costs
150 to 300 KB, because every commit rewrites `index.json` and the
`items` tree; maintenance folds those into deltas once slack passes the
cap.

## Footprint

| Tree | 0.6.x baseline | 0.7.0 |
| --- | --- | --- |
| `src/todo_db` Python | 8,563 lines + 237 migration lines | 3,101 lines, no migrations |
| `tests/` | 7,870 lines | 1,134 lines |
| MCP tools | 26 default / 38 full | 9 + `get_instructions` |
| Runtime dependencies | libsql, cryptography, pyyaml (extras) | none beyond the `mcp` extra |
| Dev dependencies | + libsql, cryptography, pyyaml | + tiktoken (budget measurement) |

Test lines fell because the removed suite tested the removed product
(hosted backends, audit chain, findings, workflow gates, SQL budgets).
Retained behaviors — readiness, priority ordering, cycle rejection,
one-winner claims, bounded output, migration mapping — are ported into
the new suite, not deleted. 75 tests pass: `uv run pytest -q`.
