# Release gates

Artifact correctness is not evidence of usability.

v0.4.2 was tagged, released, and pushed into a downstream consumer with every
gate green: six merged PRs, Python 3.10 and 3.13 CI, the Pi adapter CI, 219
tests, Ruff, scope checks, isolated wheel and sdist consumer smoke tests, and
SHA-256 verification of the downloaded release assets. Every one of those was
true and none of them executed the changed authentication path. The first time a
person used the release it failed. This page exists so that cannot recur
silently.

## Trigger

Both gates below are mandatory for a release whose diff touches any of:

- the publication protocol in `src/todo_db/git_backend.py`, including tip
  resolution, conflict classification, retries, and reconciliation;
- the state schema, validation, or claim rules in `src/todo_db/store.py`;
- the agent surface in `src/todo_db/service.py` or `src/todo_db/mcp/`;
- the floor CLI in `src/todo_db/cli.py` or migration in
  `src/todo_db/migrate.py`.

Releases that touch none of these are not subject to the downstream consumer
gate. When in doubt, run both; they cost minutes.

## Gate 1: scratch state-branch lifecycle

With the release candidate installed, run a full lifecycle against a scratch
bare remote — bootstrap, create, take, renew, finish, plus a conflicting
write and an offline read:

```sh
git init --bare /tmp/gate-state.git
todo-db bootstrap --state-remote /tmp/gate-state.git
todo-db validate --state-remote /tmp/gate-state.git
```

then drive one `take` → `finish` round through the MCP server. A gate that
passes without touching the publication path the release changed is the
v0.4.2 failure mode; `--help` output is not evidence.

## Gate 2: real downstream consumer

Upgrade one real downstream consumer to the release candidate and run one real
floor-CLI command against its state branch, in a session that was not specially
prepared for the test.

"One real command" means a surviving `todo-db` floor verb that does real work
— `todo-db validate`, `todo-db list`, or `todo-db recover --limit 5` — not
`--help` and not `--version`. Agent verbs live only on the MCP server
(`todo-db-mcp`), which is not a release gate — the gate exercises the floor
CLI a bootstrap or CI step actually runs.

## Ordering

Both gates run **before** the tag.

A release is not tagged, published, or announced to a consumer until both have
passed. A failed gate blocks the tag; it is not recorded as a follow-up item and
worked around. v0.4.2 dispatched its downstream upgrade after publication, which
is why the defect reached the consumer before it reached anyone who could stop
the release.

## After the tag

A GitHub release alone is not completed distribution. For every release, the
Publish run must pass `pypi-publish` and `distribution-verify`. The latter compares
the wheel and sdist on GitHub and PyPI with the original build artifacts, then
installs `todo-db[mcp]==$VERSION` from PyPI without cache or local configuration
in a fresh environment. Both entry points must report the release version and
a real MCP initialize/tools-list exchange must succeed against a scratch remote.
Missing files, yanked files, differing bytes, install failures, or a failed or
pending publisher block distribution completion and consumer rollout.

To rerun the index verification locally against the original downloaded build
artifact directory:

```sh
python3 scripts/verify_index_release.py "$VERSION" /path/to/original/dist
```

This post-publication check supplements, never replaces, the pre-tag gates.
Record its run URL and artifact hashes before announcing completed distribution.
Only after it passes:

1. **Global tool.** Keep the current receipt and a checksum-verified wheel
   compatible with the active schema for rollback. Replace a URL-pinned or
   checkout installation once with an index requirement:

   ```sh
   uv tool install --force --default-index https://pypi.org/simple 'todo-db[mcp]>=0.14.0'
   todo-db --version
   todo-db-mcp --version
   ```

   Later releases use `uv tool upgrade todo-db`. Check that the saved requirement
   in `$(uv tool dir)/todo-db/uv-receipt.toml` retains the `mcp` extra and no
   direct `todo-db` URL. A direct wheel URL stays pinned even when newer GitHub
   releases exist. A checkout install copies the tree at install time.

   For rollback, use the immutable-wheel command in the README with the saved
   compatible version and digest. Never roll a client back below the active
   schema's minimum version or downgrade authoritative state. If index
   verification fails, leave the existing working installation untouched.
2. **Vendored consumers.** A repository that vendors the wheel, such as
   BenchBox's `_project/scripts/vendor/`, needs its own upgrade PR.
3. **Skill text.** When the release changes what agents should do, merge the
   `todo` skill change in skill-sync-skills, then advance every pin that
   serves it: each project's `skill-sync.conf` (see
   [skill-deployment.md](skill-deployment.md)) and the global loader folders
   (`~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`). For the global
   folders, bump the template rev (`deployment/global/skill-sync.conf`)
   through review in skill-sync-skills, set the same rev in
   `~/.skill-sync-deployment/skill-sync.conf`, then run `tools/skill-sync
   apply`, `verify` and `check` with `-C ~ -f
   ~/.skill-sync-deployment/skill-sync.conf`. There is no snapshot or symlink
   repoint; the skill-sync-skills README has the details.
4. **Running sessions.** An MCP server keeps the version it started with.
   Restart open sessions, then check the server: from 0.11.0, its
   `get_instructions` answer includes a "Bound store" section.

## PyPI publisher setup and recovery

The PyPI account owner registers a pending trusted publisher at
<https://pypi.org/manage/account/publishing/> with project `todo-db`, GitHub
owner `joeharris76`, repository `todo-db`, workflow filename `publish.yml`,
and environment `pypi`. If the project already exists, verify ownership and
configure its active publisher instead. Match these fields to the workflow;
never claim an unrelated project, add a persistent API token, or remove
environment protections to bypass an authentication failure.

After registration, re-run only the failed `pypi-publish` job in the original
tag's Publish run while its `dist` artifact is retained (currently seven days).
For example, for the existing 0.14.0 release:

```sh
gh run rerun 41379383917 --failed --repo joeharris76/todo-db
```

A manual dispatch on `main` does not publish: publishing jobs require a tag
ref. Tag dispatches must also pass the manifest/tag version check. If the
artifact has expired, stop before upload: rebuild the original tag separately,
compare both distribution hashes with the existing GitHub assets, and obtain
an explicit operator decision if bytes differ. Never move a tag or silently
upload a different build. An already-published version must be checked for
parity rather than overwritten.

Older tag runs do not contain `distribution-verify`; run the verifier from
the current reviewed tooling against the original tag's artifacts and retain
the result. A green old Publish run alone does not satisfy the new gate.

## Before an irreversible schema cutover

The operator must inventory every client with access to the affected state
branch, including global tools, vendored consumers, CI, and running MCP
sessions. Record each owner, install source, client version, state remote and
branch, and whether its process was restarted or stopped. Do not record
credentials or private remote URLs in public evidence.

Publish and verify the compatible client before cutover. Upgrade each affected
client, restart its server, and verify its running version, bound store, and
schema capability; alternatively stop it and prevent restart with the old
binary. Installed metadata alone does not prove a running server was upgraded.
An unknown, unreachable, or incompatible client blocks cutover until its owner
resolves it. Keep a checksum-verified rollback client that can read the new
schema; rollback never means downgrading state.

Preserve the required explicit cutover confirmation. On `E_SCHEMA`, agents
still stop and ask a human to upgrade the package; this procedure authorizes
neither automatic migration nor bypassing the recovery policy.


## Evidence

Record in the release PR body:

- the command line used for gate 1 and its final line;
- the consumer, the floor-CLI command run for gate 2, the state branch it
  addressed, and whether it succeeded;
- the date and the operator.

Record no consumer secret and no private remote URL beyond what the release
already names.

## What does not satisfy these gates

State this plainly when reviewing a release PR. None of the following is
evidence that the release is usable:

- green CI on every matrix entry;
- a passing unit and integration suite, at any count;
- isolated wheel, sdist, or package smoke tests;
- checksum verification of downloaded release assets;
- a clean `git status` and no open PRs.

All five held for v0.4.2.

## Automation boundary

Gate 1's full run and gate 2 stay local operator steps. CI runs a scratch
state-branch smoke (bootstrap/validate/list against a disposable bare
remote) plus `uv sync --locked`, which fails the build when the committed
lockfile drifts from the manifest. CI never touches a real state branch:
routine automation must not write to authoritative task state.
