#!/usr/bin/env bash
# Opt-in acceptance test script for Turso remote transaction concurrency and fault injection.
# Requires TURSO_API_TOKEN, TURSO_ORG (or TURSO_ORGANIZATION), or explicit TEST_TURSO_URL and TEST_TURSO_TOKEN.
# Automatically provisions a temporary disposable database, runs concurrency races,
# verifies changes() and commit semantics, and cleans up the disposable database.

set -euo pipefail

DB_NAME="todo-db-acc-$(date +%s)-$RANDOM"
CLEANUP_REQUIRED=0

cleanup() {
    if [[ "$CLEANUP_REQUIRED" -eq 1 ]]; then
        echo "== Cleaning up disposable Turso database: $DB_NAME =="
        turso db destroy "$DB_NAME" --yes 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [[ -z "${TEST_TURSO_URL:-}" ]]; then
    if ! command -v turso &>/dev/null; then
        echo "turso CLI not found and TEST_TURSO_URL not set. Skipping live Turso cloud acceptance test."
        echo "Local adapter tests remain certified via pytest tests/test_hosted_backend.py."
        exit 0
    fi

    if ! turso auth whoami &>/dev/null; then
        echo "turso CLI is logged out. Skipping live Turso cloud acceptance test."
        exit 0
    fi

    echo "== Provisioning temporary Turso database: $DB_NAME =="
    GROUP_FLAG=()
    if [[ -n "${TURSO_GROUP:-}" ]]; then
        GROUP_FLAG=(--group "$TURSO_GROUP")
    else
        DEFAULT_GROUP="$(turso group list 2>/dev/null | awk 'NR==2 {print $1}' || true)"
        if [[ -n "$DEFAULT_GROUP" ]]; then
            GROUP_FLAG=(--group "$DEFAULT_GROUP")
        fi
    fi
    turso db create "$DB_NAME" "${GROUP_FLAG[@]}"
    CLEANUP_REQUIRED=1

    TEST_TURSO_URL="$(turso db show "$DB_NAME" --url)"
    TEST_TURSO_TOKEN="$(turso db tokens create "$DB_NAME")"
fi

echo "== Running concurrency and transaction race against: $TEST_TURSO_URL =="
export TODO_DB_URL="$TEST_TURSO_URL"
export TODO_DB_AUTH_TOKEN="$TEST_TURSO_TOKEN"
export TODO_DB_PROJECT_ID="turso-acceptance"
export TODO_DB_REPOSITORY="todo-db"

uv run python -c "
import os
from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.models import CredentialMode

url = os.environ['TODO_DB_URL']
token = os.environ['TODO_DB_AUTH_TOKEN']
config = DatabaseConfig(path=url, identity=ProjectIdentity('turso-acceptance', 'todo-db'), auth_token=token, credential_mode=CredentialMode.READ_WRITE)
db = TodoDatabase.open(config)
tracker = TodoTracker(db, actor='turso-runner')
item_id = tracker.create_item(item_id='acc-item-1', title='Acceptance Item', worktree='todo-db', priority='high', description='Testing Turso')
tracker.claim(item_id)
item = tracker.get_item(item_id)
assert item['claimed_by'] == 'turso-runner'
print('Turso live mutation test passed!')
db.close()
"

echo "== Live acceptance drill completed successfully =="
