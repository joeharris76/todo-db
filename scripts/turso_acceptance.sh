#!/usr/bin/env bash
# Opt-in real-primary Turso claim race. Exit 77 means not run and MUST NOT certify hosted support.
# This proves only two-connection one-winner claim behavior; commit-outcome fault injection remains a separate gate.
set -euo pipefail

DB_NAME="todo-db-acc-$(date +%s)-$RANDOM"
CLEANUP_REQUIRED=0
cleanup() {
  if [[ "$CLEANUP_REQUIRED" -eq 1 ]]; then
    turso db destroy "$DB_NAME" --yes >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

skip() {
  echo "SKIP: $1" >&2
  if [[ "${REQUIRE_TURSO_ACCEPTANCE:-0}" == "1" ]]; then exit 1; fi
  exit 77
}

if [[ -z "${TEST_TURSO_URL:-}" ]]; then
  command -v turso >/dev/null 2>&1 || skip "turso CLI is unavailable"
  turso auth whoami >/dev/null 2>&1 || skip "turso CLI is not authenticated"
  GROUP_FLAG=()
  if [[ -n "${TURSO_GROUP:-}" ]]; then GROUP_FLAG=(--group "$TURSO_GROUP"); fi
  turso db create "$DB_NAME" "${GROUP_FLAG[@]}"
  CLEANUP_REQUIRED=1
  TEST_TURSO_URL="$(turso db show "$DB_NAME" --url)"
  TEST_TURSO_TOKEN="$(turso db tokens create "$DB_NAME")"
fi
[[ -n "${TEST_TURSO_TOKEN:-}" ]] || skip "TEST_TURSO_TOKEN is required with TEST_TURSO_URL"

export TODO_DB_URL="$TEST_TURSO_URL"
export TODO_DB_AUTH_TOKEN="$TEST_TURSO_TOKEN"
uv run python <<'PY'
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.models import CredentialMode

url = os.environ["TODO_DB_URL"]
token = os.environ["TODO_DB_AUTH_TOKEN"]
identity = ProjectIdentity("turso-acceptance", "todo-db")
config = DatabaseConfig(path=url, identity=identity, auth_token=token, credential_mode=CredentialMode.READ_WRITE)
with TodoDatabase.open(config) as database:
    tracker = TodoTracker(database, actor="setup")
    tracker.create_item(
        item_id="contested-item",
        title="Contested acceptance item",
        worktree="todo-db",
        priority="high",
        description="Real Turso two-connection claim acceptance",
    )

worker = r'''
import json, os, sys, time
from pathlib import Path
from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.models import CredentialMode
while not Path(sys.argv[2]).exists(): time.sleep(0.01)
config = DatabaseConfig(path=os.environ["TODO_DB_URL"], identity=ProjectIdentity("turso-acceptance", "todo-db"), auth_token=os.environ["TODO_DB_AUTH_TOKEN"], credential_mode=CredentialMode.READ_WRITE)
try:
    with TodoDatabase.open(config) as db:
        TodoTracker(db, actor=sys.argv[1]).claim("contested-item")
    print(json.dumps({"actor": sys.argv[1], "won": True}))
except Exception as exc:
    print(json.dumps({"actor": sys.argv[1], "won": False, "error": str(exc)}))
'''
with tempfile.TemporaryDirectory() as directory:
    start = Path(directory) / "start"
    processes = [
        subprocess.Popen([sys.executable, "-c", worker, actor, str(start)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for actor in ("actor-a", "actor-b")
    ]
    start.touch()
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode != 0:
            raise SystemExit(f"worker process failed: {stderr}")
        results.append(json.loads(stdout))

winners = [result for result in results if result["won"]]
if len(winners) != 1:
    raise SystemExit(f"expected exactly one Turso claim winner, observed: {results}")
with TodoDatabase.open(config) as database:
    item = TodoTracker(database, actor="audit").get_item("contested-item")
    assert item["claimed_by"] == winners[0]["actor"], (item, results)
    database.verify_audit()
print(json.dumps({"status": "pass", "evidence": "real-primary two-connection one-winner claim", "results": results}))
PY

echo "PASS: real Turso two-connection claim race. Commit-outcome fault injection is NOT certified."
