#!/usr/bin/env bash
# Hosted authentication acceptance: prove the ADR 0005 G1 criterion.
#
#   On a machine provisioned once, a new session resolves a hosted credential
#   with zero interactive steps.
#
# This is the only check in the repository that can fail the way an operator
# fails. Every other gate — unit tests, lint, CI, package smoke tests, artifact
# checksums — passed for v0.4.2 while hosted authentication was unusable,
# because none of them ran with the environment credentials removed.
#
# The harness deliberately unsets TODO_DB_AUTH_TOKEN and TODO_DB_RO_AUTH_TOKEN
# before running todo-db. An inherited shell or CI credential would otherwise
# satisfy the run and prove nothing about the provider.
#
# Usage:
#   scripts/hosted_auth_acceptance.sh            # skip (exit 77) when unconfigured
#   scripts/hosted_auth_acceptance.sh --require  # unconfigured is a failure
#
# Configure with TODO_DB_ACCEPTANCE_URL (or TODO_DB_URL) and
# TODO_DB_CREDENTIAL_COMMAND.
set -euo pipefail
set +x  # never trace: the provider's stdout is a bearer token

REQUIRE=0
for arg in "$@"; do
  case "$arg" in
    --require) REQUIRE=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

skip() {
  echo "SKIP: $1" >&2
  if [[ "$REQUIRE" -eq 1 ]]; then
    echo "FAIL: --require was given, so an unconfigured harness is a failure" >&2
    exit 1
  fi
  exit 77
}

fail() { echo "FAIL: $1" >&2; exit 1; }

TARGET="${TODO_DB_ACCEPTANCE_URL:-${TODO_DB_URL:-}}"
[[ -n "$TARGET" ]] || skip "set TODO_DB_ACCEPTANCE_URL (or TODO_DB_URL) to a hosted database"
[[ -n "${TODO_DB_CREDENTIAL_COMMAND:-}" ]] || skip "set TODO_DB_CREDENTIAL_COMMAND; see docs/operations/hosted-credentials.md, Provision once"

TODO_DB_TOOL_CMD=(todo-db)
if ! command -v todo-db >/dev/null 2>&1; then
  command -v uv >/dev/null 2>&1 || skip "neither todo-db nor uv is on PATH"
  TODO_DB_TOOL_CMD=(uv run todo-db)
fi

# The point of the harness: run as a session that was never handed a credential.
unset TODO_DB_AUTH_TOKEN
unset TODO_DB_RO_AUTH_TOKEN
unset TODO_DB_CONFIG
export TODO_DB_AUTH_CONTRACT=v2

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== doctor with no injected credential"
if ! "${TODO_DB_TOOL_CMD[@]}" --db "$TARGET" doctor --json >"$WORK/doctor.json" 2>"$WORK/doctor.err"; then
  sed -e 's/[A-Za-z0-9_-]\{40,\}/[REDACTED]/g' "$WORK/doctor.err" >&2
  fail "doctor did not succeed without an injected credential"
fi

python3 - "$WORK/doctor.json" <<'PY' || fail "doctor did not resolve the credential from the provider"
import json, sys

payload = json.load(open(sys.argv[1]))
checks = {check["name"]: check for check in payload.get("checks", [])}
database = checks.get("database")
if database is None:
    print("no database check in doctor output", file=sys.stderr)
    raise SystemExit(1)
source = database.get("source")
if source != "TODO_DB_CREDENTIAL_COMMAND":
    print(f"credential source was {source!r}, expected TODO_DB_CREDENTIAL_COMMAND", file=sys.stderr)
    raise SystemExit(1)
print(f"   source={source} capability={database.get('capability')}")
PY

echo "== ordinary read with no injected credential"
"${TODO_DB_TOOL_CMD[@]}" --db "$TARGET" audit verify >"$WORK/audit.txt" 2>"$WORK/audit.err" \
  || { sed -e 's/[A-Za-z0-9_-]\{40,\}/[REDACTED]/g' "$WORK/audit.err" >&2; fail "an ordinary read failed"; }

# No output we produced may contain anything token-shaped. The harness never
# retrieves the credential itself, so there is nothing here to leak either.
if grep -qE '[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{20,}' \
     "$WORK/doctor.json" "$WORK/doctor.err" "$WORK/audit.txt" "$WORK/audit.err"; then
  fail "output contained a token-shaped string"
fi

echo "PASS: hosted authentication resolved with zero interactive steps"
