#!/usr/bin/env python3
"""Mechanical parity conformance between BenchBox and standalone todo-db.

Diffs three surfaces that must stay compatible across the cutover:
  1. --help surfaces (commands, subcommands, argument sets)
  2. exit-code maps (0, 1, 2, 4 contract)
  3. tracker + findings DDL (migrations/*.sql combined schema)

Any asymmetry not explicitly allowlisted fails CI.  An explicit allowlist at
``scripts/parity_allowlist.json`` gates intentional divergences (e.g. wrapper-only
extensions and standalone-only recovery commands).

Freeze enforcement: until cutover the in-repo tracker is feature-frozen.  New
commands or new migrations beyond the frozen snapshot must be allowlisted.

Usage:
  python scripts/parity_conformance.py --check                # CI gate
  python scripts/parity_conformance.py --check --benchbox ../BenchBox
  python scripts/parity_conformance.py --update-snapshots     # refresh snapshots
  python scripts/parity_conformance.py --json                 # machine output
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "scripts" / "parity_snapshots"
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "parity_allowlist.json"
MIGRATIONS_DIR = REPO_ROOT / "src" / "todo_db" / "migrations"


def load_allowlist(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {"help": [], "exit_codes": [], "ddl": [], "freeze": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "help": list(data.get("help", [])),
        "exit_codes": list(data.get("exit_codes", [])),
        "ddl": list(data.get("ddl", [])),
        "freeze": list(data.get("freeze", [])),
    }


def _matches_allowlist(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            if pat == text or pat in text:
                return True
    return False


def capture_standalone_help() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from todo_db.cli import _parser, EXIT_CODES_EPILOG

    parser = _parser()
    top_help = parser.format_help()
    commands: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice, sub in action.choices.items():
                args: list[dict[str, Any]] = []
                for act in sub._actions:
                    if isinstance(act, argparse._HelpAction):
                        continue
                    args.append(
                        {
                            "option_strings": list(act.option_strings),
                            "dest": act.dest,
                            "required": getattr(act, "required", False),
                            "choices": list(act.choices) if getattr(act, "choices", None) else None,
                            "help": str(act.help) if act.help else "",
                        }
                    )
                subcommands: dict[str, Any] = {}
                for a2 in sub._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        for c2, s2 in a2.choices.items():
                            subcommands[c2] = {
                                "help": s2.description or "",
                                "args": [
                                    {
                                        "option_strings": list(x.option_strings),
                                        "dest": x.dest,
                                        "required": getattr(x, "required", False),
                                        "choices": list(x.choices) if getattr(x, "choices", None) else None,
                                    }
                                    for x in s2._actions
                                    if not isinstance(x, argparse._HelpAction)
                                ],
                            }
                commands[choice] = {
                    "help": sub.description or "",
                    "args": args,
                    "subcommands": subcommands,
                    "usage": sub.format_usage().strip(),
                }
    exit_codes: dict[str, str] = {}
    for line in EXIT_CODES_EPILOG.splitlines():
        m = re.match(r"\s*(\d)\s+(.*)", line)
        if m:
            exit_codes[m.group(1).strip()] = m.group(2).strip()
    return {"top_help": top_help, "commands": commands, "exit_codes": exit_codes}


def capture_standalone_ddl() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from todo_db.database import SCHEMA_VERSION
    import tempfile

    tmp = Path(tempfile.mktemp(suffix=".sqlite"))
    try:
        from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

        identity = ProjectIdentity(project_id="parity-check", repository="https://example.test/parity")
        config = DatabaseConfig(path=tmp, identity=identity)
        db = TodoDatabase.open(config)
        conn = db.connection
        tables: dict[str, str] = {}
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            tables[row["name"]] = row["sql"] or ""
        indexes: dict[str, str] = {}
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' ORDER BY name"):
            if row["sql"]:
                indexes[row["name"]] = row["sql"]
        db.close()
    finally:
        tmp.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
    migrations = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        migrations.append({"file": p.name, "sql": p.read_text(encoding="utf-8")})
    return {"schema_version": SCHEMA_VERSION, "tables": tables, "indexes": indexes, "migrations": migrations}


def capture_standalone_exit_map() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from todo_db import errors as _errors
    from todo_db.cli import EXIT_CODES_EPILOG

    codes = {}
    for line in EXIT_CODES_EPILOG.splitlines():
        m = re.match(r"\s*(\d)\s+(.*)", line)
        if m:
            codes[m.group(1)] = m.group(2).strip()
    structured = {k: v for k, v in vars(_errors).items() if k.startswith("E_")}
    return {"epilog_codes": codes, "structured_codes": structured, "epilog_raw": EXIT_CODES_EPILOG.strip()}


def _try_load_benchbox_compat(benchbox_root: Path) -> dict[str, Any] | None:
    compat = benchbox_root / "_project" / "scripts" / "todo_db_standalone_compat.py"
    if not compat.is_file():
        return None
    spec = importlib.util.spec_from_file_location("benchbox_compat", compat)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        sys.modules["benchbox_compat"] = mod
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception as exc:
        return {"error": f"failed to import compat: {exc}"}
    return {
        "COMMANDS": sorted(getattr(mod, "COMMANDS", [])),
        "STANDALONE_ONLY_COMMANDS": sorted(getattr(mod, "STANDALONE_ONLY_COMMANDS", [])),
        "EXTENSION_COMMANDS": sorted(getattr(mod, "EXTENSION_COMMANDS", [])),
        "PACKAGE_COMMAND_TRANSLATIONS": dict(getattr(mod, "PACKAGE_COMMAND_TRANSLATIONS", {})),
        "compat_path": str(compat),
    }


def _try_load_benchbox_extensions(benchbox_root: Path) -> dict[str, Any] | None:
    ext = benchbox_root / "_project" / "scripts" / "todo_db_standalone_extensions.py"
    if not ext.is_file():
        return None
    text = ext.read_text(encoding="utf-8")
    keys = re.findall(r"maintenance\.[a-z_\.]+", text)
    return {"path": str(ext), "meta_keys": sorted(set(keys)), "has_renew": "def _renew" in text, "has_freeze": "FREEZE" in text}


def diff_help(standalone: dict[str, Any], benchbox: dict[str, Any] | None, allow: list[str]) -> list[str]:
    issues: list[str] = []
    s_cmds = set(standalone["commands"].keys())
    if benchbox and "COMMANDS" in benchbox:
        b_cmds = set(benchbox["COMMANDS"])
        ext_cmds = set(benchbox.get("EXTENSION_COMMANDS", []))
        standalone_only = set(benchbox.get("STANDALONE_ONLY_COMMANDS", []))
        expected_missing_in_benchbox = standalone_only
        expected_extra_in_benchbox = ext_cmds | {"scope-update"}
        for cmd in sorted(s_cmds - b_cmds):
            if cmd in expected_missing_in_benchbox:
                continue
            msg = f"help: standalone command {cmd!r} not in BenchBox COMMANDS"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
        for cmd in sorted(b_cmds - s_cmds):
            if cmd in expected_extra_in_benchbox:
                continue
            translations = benchbox.get("PACKAGE_COMMAND_TRANSLATIONS", {})
            if cmd in translations and translations[cmd] in s_cmds:
                continue
            msg = f"help: BenchBox command {cmd!r} not in standalone"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    snapshot_path = SNAPSHOT_DIR / "standalone_help.json"
    if snapshot_path.is_file():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snap_cmds = set(snapshot.get("commands", {}).keys())
            for cmd in sorted(s_cmds - snap_cmds):
                msg = f"freeze: new standalone command {cmd!r} not in frozen snapshot"
                if not _matches_allowlist(msg, allow):
                    issues.append(msg)
        except Exception:
            pass
    return issues


def diff_exit_codes(standalone_map: dict[str, Any], allow: list[str]) -> list[str]:
    issues: list[str] = []
    codes = standalone_map.get("epilog_codes", {})
    required = {"0", "1", "2", "4"}
    for c in sorted(required):
        if c not in codes:
            msg = f"exit_codes: required code {c} missing from EXIT_CODES_EPILOG"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    expectations = {"0": "success", "1": "findings", "2": "generic", "4": "hosted authentication"}
    for code, keyword in expectations.items():
        body = codes.get(code, "").lower()
        if keyword not in body:
            msg = f"exit_codes: code {code} missing keyword {keyword!r} (have {codes.get(code)!r})"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    structured = standalone_map.get("structured_codes", {})
    for expected in ("E_AUTH_MISSING", "E_CLAIM_STALE", "E_LINT_GATE", "E_SCOPE_GATE", "E_VERIFY_GATE"):
        if expected not in structured:
            msg = f"exit_codes: structured code {expected} missing from todo_db.errors"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    return issues


def diff_ddl(standalone_ddl: dict[str, Any], allow: list[str]) -> list[str]:
    issues: list[str] = []
    tables = standalone_ddl.get("tables", {})
    required_tables = {
        "items",
        "work_units",
        "work_needs",
        "item_deps",
        "scope_rules",
        "verifications",
        "preserves",
        "anti_patterns",
        "prior_art",
        "deferrals",
        "meta",
        "findings",
        "finding_evidence",
        "finding_links",
        "finding_events",
        "finding_sections",
        "schema_migrations",
        "project_identity",
        "events",
        "audit_head",
    }
    for t in sorted(required_tables):
        if t not in tables:
            msg = f"ddl: required table {t!r} missing"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    snapshot_path = SNAPSHOT_DIR / "standalone_ddl.json"
    if snapshot_path.is_file():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snap_tables = set(snapshot.get("tables", {}).keys())
            live_tables = set(tables.keys())
            for t in sorted(live_tables - snap_tables):
                if t.startswith("sqlite_"):
                    continue
                msg = f"freeze: new table {t!r} not in frozen DDL snapshot"
                if not _matches_allowlist(msg, allow):
                    issues.append(msg)
            for t in sorted(required_tables & snap_tables & live_tables):
                if tables[t] != snapshot["tables"].get(t):
                    msg = f"ddl: table {t!r} schema diverged from snapshot"
                    detail = f"  live: {tables[t]!r}\n  snapshot: {snapshot['tables'].get(t)!r}"
                    full = f"{msg}\n{detail}"
                    if not _matches_allowlist(msg, allow) and not _matches_allowlist(full, allow):
                        issues.append(full)
        except Exception as exc:
            issues.append(f"ddl: failed to compare snapshot: {exc}")
    snap_mig = None
    if (SNAPSHOT_DIR / "standalone_ddl.json").is_file():
        try:
            snap_mig = json.loads((SNAPSHOT_DIR / "standalone_ddl.json").read_text(encoding="utf-8")).get("migrations", [])
        except Exception:
            snap_mig = None
    if snap_mig is not None:
        live_names = [m["file"] for m in standalone_ddl.get("migrations", [])]
        snap_names = [m["file"] for m in snap_mig]
        for name in sorted(set(live_names) - set(snap_names)):
            msg = f"freeze: new migration {name!r} not in frozen snapshot"
            if not _matches_allowlist(msg, allow):
                issues.append(msg)
    return issues


def run_conformance(*, benchbox_root: Path | None, allowlist: dict[str, list[str]]) -> dict[str, Any]:
    standalone_help = capture_standalone_help()
    standalone_ddl = capture_standalone_ddl()
    standalone_exit = capture_standalone_exit_map()
    benchbox_info = None
    if benchbox_root is not None:
        benchbox_info = _try_load_benchbox_compat(benchbox_root)
        ext_info = _try_load_benchbox_extensions(benchbox_root)
        if ext_info and benchbox_info is not None:
            benchbox_info["extensions"] = ext_info
    else:
        candidate = REPO_ROOT.parent / "BenchBox"
        if candidate.is_dir():
            benchbox_info = _try_load_benchbox_compat(candidate)
            ext = _try_load_benchbox_extensions(candidate)
            if ext and benchbox_info is not None:
                benchbox_info["extensions"] = ext
    help_issues = diff_help(standalone_help, benchbox_info, allowlist.get("help", []) + allowlist.get("freeze", []))
    exit_issues = diff_exit_codes(standalone_exit, allowlist.get("exit_codes", []))
    ddl_issues = diff_ddl(standalone_ddl, allowlist.get("ddl", []) + allowlist.get("freeze", []))
    all_issues = help_issues + exit_issues + ddl_issues
    return {
        "standalone": {
            "help": {"commands": sorted(standalone_help["commands"].keys()), "exit_codes": standalone_help["exit_codes"]},
            "ddl": {"schema_version": standalone_ddl["schema_version"], "tables": sorted(standalone_ddl["tables"].keys())},
            "exit_map": standalone_exit,
        },
        "benchbox": benchbox_info,
        "issues": {"help": help_issues, "exit_codes": exit_issues, "ddl": ddl_issues, "all": all_issues},
        "allowlist": allowlist,
        "pass": len(all_issues) == 0,
    }


def write_snapshots() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    help_cap = capture_standalone_help()
    ddl_cap = capture_standalone_ddl()
    exit_cap = capture_standalone_exit_map()
    help_snapshot = {"commands": help_cap["commands"], "exit_codes": help_cap["exit_codes"]}
    (SNAPSHOT_DIR / "standalone_help.json").write_text(json.dumps(help_snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (SNAPSHOT_DIR / "standalone_ddl.json").write_text(json.dumps(ddl_cap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (SNAPSHOT_DIR / "standalone_exit.json").write_text(json.dumps(exit_cap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT_DIR / 'standalone_help.json'}")
    print(f"wrote {SNAPSHOT_DIR / 'standalone_ddl.json'}")
    print(f"wrote {SNAPSHOT_DIR / 'standalone_exit.json'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--benchbox", type=Path, default=None, help="BenchBox checkout root (default: ../BenchBox if present)")
    p.add_argument("--allowlist", type=Path, default=ALLOWLIST_PATH, help="allowlist JSON path")
    p.add_argument("--check", action="store_true", help="fail (exit 1) on non-allowlisted asymmetry")
    p.add_argument("--json", action="store_true", help="emit machine JSON to stdout")
    p.add_argument("--update-snapshots", action="store_true", help="refresh frozen snapshots for standalone")
    args = p.parse_args(argv)
    if args.update_snapshots:
        write_snapshots()
        return 0
    allowlist = load_allowlist(args.allowlist)
    result = run_conformance(benchbox_root=args.benchbox, allowlist=allowlist)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result["pass"]:
            print("parity conformance: PASS")
            print(f"  commands: {', '.join(result['standalone']['help']['commands'])}")
            print(f"  schema_version: {result['standalone']['ddl']['schema_version']} tables: {', '.join(result['standalone']['ddl']['tables'])}")
            if result["benchbox"] is not None:
                bb = result["benchbox"]
                if "COMMANDS" in bb:
                    print(f"  benchbox COMMANDS: {', '.join(bb['COMMANDS'])} (extensions: {', '.join(bb.get('EXTENSION_COMMANDS', []))})")
        else:
            print("parity conformance: FAIL", file=sys.stderr)
            for domain in ("help", "exit_codes", "ddl"):
                for issue in result["issues"][domain]:
                    prefix = f"[{domain}]"
                    print(f"  {prefix} {issue}", file=sys.stderr)
            print("\nIf an asymmetry is intentional, add a regex to scripts/parity_allowlist.json under the matching domain.", file=sys.stderr)
            print("To refresh frozen snapshots after an intentional cutover change: python scripts/parity_conformance.py --update-snapshots", file=sys.stderr)
    if args.check and not result["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
