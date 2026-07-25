#!/usr/bin/env bash
#
# Live Turso acceptance for the hosted todo-db path.
#
# Provisions a THROWAWAY Turso database with the `turso` CLI, exercises the
# real hosted lifecycle end-to-end through `uv run todo-db`, then destroys the
# database on exit. This costs real (if small) Turso resources -- run it
# deliberately, never from CI-by-default.
#
# Usage:
#   scripts/turso_acceptance.sh [--db-name NAME] [--keep]
#
#   --db-name NAME  use NAME instead of a random todo-db-accept-* name
#   --keep          skip destroying the database (and keep the temp workdir)
#
# Requirements: an authenticated `turso` CLI (turso auth login), uv, and this
# checkout. The auth token is minted per run, kept in environment variables
# only, and never echoed; do not add `set -x` to this script.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="todo-db-accept-${RANDOM}${RANDOM}"
KEEP=0
CREATED=0
WORK_DIR=""

PROJECT_ID="todo-db-acceptance"
REPOSITORY="https://github.com/joeharris76/todo-db"
ITEM_ID="accept-item"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --db-name)
      [ $# -ge 2 ] || { echo "error: --db-name requires a value" >&2; exit 2; }
      DB_NAME="$2"
      shift 2
      ;;
    --keep)
      KEEP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

cleanup() {
  status=$?
  if [ "$CREATED" -eq 1 ] && [ "$KEEP" -eq 0 ]; then
    echo "==> destroying throwaway database ${DB_NAME}"
    turso db destroy --yes "$DB_NAME" >/dev/null 2>&1 || echo "warning: failed to destroy ${DB_NAME}; remove it manually" >&2
  elif [ "$CREATED" -eq 1 ]; then
    echo "==> keeping database ${DB_NAME} (--keep)"
  fi
  if [ -n "$WORK_DIR" ] && [ "$KEEP" -eq 0 ]; then
    rm -rf "$WORK_DIR"
  elif [ -n "$WORK_DIR" ]; then
    echo "==> keeping workdir ${WORK_DIR} (--keep)"
  fi
  exit "$status"
}
trap cleanup EXIT

command -v turso >/dev/null 2>&1 || fail "the turso CLI is not installed (https://docs.turso.tech/cli)"
command -v uv >/dev/null 2>&1 || fail "uv is not installed"
turso auth whoami >/dev/null 2>&1 || fail "the turso CLI is not authenticated; run 'turso auth login' first"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/todo-db-accept.XXXXXX")"
REPLICA="$WORK_DIR/replica.db"
DRAFTS_DIR="$WORK_DIR/drafts"
EXPORT_JSON="$WORK_DIR/export.json"

echo "==> creating throwaway database ${DB_NAME}"
turso db create "$DB_NAME" >/dev/null || fail "turso db create ${DB_NAME} failed"
CREATED=1

DB_URL="$(turso db show --url "$DB_NAME")"
case "$DB_URL" in
  libsql://*) ;;
  *) fail "unexpected database URL scheme for ${DB_NAME} (want libsql://)" ;;
esac
echo "==> database url: ${DB_URL}"

echo "==> minting an auth token (never echoed)"
TODO_DB_AUTH_TOKEN="$(turso db tokens create "$DB_NAME")"
[ -n "$TODO_DB_AUTH_TOKEN" ] || fail "turso db tokens create returned an empty token"
export TODO_DB_AUTH_TOKEN
# Read-only commands (export, audit verify, finding show) use the RO variable;
# the full-access token covers both for this throwaway database.
export TODO_DB_RO_AUTH_TOKEN="$TODO_DB_AUTH_TOKEN"

tdb() {
  uv run --project "$REPO_ROOT" todo-db "$@"
}

# Write commands connect through the embedded replica; reads go to the primary.
WRITE_ARGS=(--db "$DB_URL" --replica "$REPLICA" --actor turso-acceptance
  --project-id "$PROJECT_ID" --repository "$REPOSITORY")
READ_ARGS=(--db "$DB_URL" --actor turso-acceptance
  --project-id "$PROJECT_ID" --repository "$REPOSITORY")

echo "==> init"
tdb "${WRITE_ARGS[@]}" init

echo "==> create ${ITEM_ID}"
tdb "${WRITE_ARGS[@]}" create "$ITEM_ID" \
  --title "Hosted acceptance item" \
  --worktree todo-db \
  --priority medium \
  --description "Exercises the real Turso hosted path end-to-end." \
  --work "w0:Run the hosted acceptance lifecycle"

echo "==> claim / done / complete"
tdb "${WRITE_ARGS[@]}" claim "$ITEM_ID"
tdb "${WRITE_ARGS[@]}" "done" "$ITEM_ID" w0 --evidence "turso_acceptance.sh live run"
tdb "${WRITE_ARGS[@]}" complete "$ITEM_ID"

echo "==> finding create (draft only)"
CREATE_OUT="$(tdb finding create \
  --title "Hosted acceptance finding" \
  --finding-kind framework-gap \
  --review-context "turso acceptance" \
  --gate class-not-instance \
  --drafts-dir "$DRAFTS_DIR" \
  --project-id "$PROJECT_ID" --repository "$REPOSITORY")"
DRAFT_PATH="$(printf '%s\n' "$CREATE_OUT" | sed -n 's/^Recorded: //p')"
[ -n "$DRAFT_PATH" ] || fail "finding create did not report a draft path"
FINDING_ID="$(basename "$DRAFT_PATH" .md)"

echo "==> finding sync"
tdb "${WRITE_ARGS[@]}" finding sync --drafts-dir "$DRAFTS_DIR"

echo "==> finding show ${FINDING_ID}"
tdb "${READ_ARGS[@]}" finding show "$FINDING_ID" >/dev/null

echo "==> audit verify"
tdb "${READ_ARGS[@]}" audit verify

echo "==> export"
tdb "${READ_ARGS[@]}" export --output "$EXPORT_JSON"

echo "==> assert schema_migrations contains version 4"
uv run --project "$REPO_ROOT" python - "$EXPORT_JSON" <<'PYCHECK'
import json
import sys

envelope = json.load(open(sys.argv[1], encoding="utf-8"))
versions = sorted(row["version"] for row in envelope["tables"]["schema_migrations"])
assert 4 in versions, f"schema_migrations is missing version 4: {versions}"
item_states = {row["id"]: row["state"] for row in envelope["tables"]["items"]}
assert item_states.get("accept-item") == "done", f"unexpected item states: {item_states}"
findings = [row["id"] for row in envelope["tables"]["findings"]]
assert findings, "expected at least one landed finding"
print(f"schema_migrations versions: {versions}")
PYCHECK

echo
echo "PASS: hosted acceptance succeeded against ${DB_NAME}"
echo "  - init, create, claim, done, complete: ${ITEM_ID} reached state=done"
echo "  - finding draft ${FINDING_ID} synced and readable"
echo "  - audit chain verified; export written; schema v4 confirmed"
