# Test doubles: what each one cannot catch

A double written to satisfy the code under test agrees with that code by
construction. This page records, for each double in `tests/`, what would still
pass if the implementation were subtly wrong, so the next reader does not have
to re-derive it.

It exists because two defects in one helper once let twelve provider tests pass
against a resolver that could not drive a real secret store: the suite was
green throughout and an external reviewer found it.

## The question to ask

Not "does this test pass?" but **"what would still pass if the implementation
were wrong?"** Where the answer is "something that matters", make the double
strict. Where it is "nothing that matters", record why below.

## Current doubles

The 0.7.0 suite holds no test doubles. Git races, conflicts, offline reads,
and lost replies are proved against disposable local bare remotes with real
`git` subprocesses (`tests/test_git_pub.py`, `tests/test_service.py`), and
the MCP surface is proved through real stdio and in-memory sessions
(`tests/test_mcp_stdio.py`, `tests/test_mcp_slim.py`). What the suite cannot
catch is GitHub-specific behaviour — branch protection rules, permission
shapes, credential helpers — which local bare remotes do not model. Those
assumptions are recorded in the PR body and release notes, not asserted here.

## Rule for new doubles

1. Reject unexpected arguments, keywords, and call shapes rather than tolerating
   them.
2. When a double stands in for an external tool, write down which real tool it
   imitates and where that tool is actually exercised.
3. Prefer a disposable real thing (a bare repo in `tmp_path`, a subprocess
   server) over a double when the cost is seconds.
