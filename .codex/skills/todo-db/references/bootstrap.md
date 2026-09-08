# Bootstrap the tracker

Set up tracker state for a repository. State lives on a dedicated Git
branch, outside the code worktree.

## Create the state branch

A human runs, from anywhere with Git access to the remote:

```sh
todo-db bootstrap --state-remote <git-url-or-path> [--state-branch todo-state] --write-config
```

`--write-config` writes `.todo-db/config.json` (state remote + branch)
in the repo root for server discovery. Bootstrap refuses to overwrite an
existing state branch.

## Point the agent at it

- MCP server: `--state-remote` / `--state-branch` / `--cache-dir` flags,
  `TODO_DB_STATE_REMOTE` / `TODO_DB_STATE_BRANCH` / `TODO_DB_CACHE_DIR`
  env, or the discovered `.todo-db/config.json` — in that order.
- One server instance is one worker identity (`--actor`, else
  `TODO_DB_ACTOR`, else the client name from the MCP handshake).

## Preflight verification

1. Run `todo-db validate` (human/CI) or `list_items` (agent).
2. A missing branch means bootstrap has not run yet — say so, do not
   improvise state.
3. The state branch shares its repository's access and visibility. Never
   publish credentials to it and never assume it is private.
