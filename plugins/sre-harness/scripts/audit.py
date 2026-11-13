#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Work with the audit log of an incident.

The log is written by the audit hook (audit_hook.py) while the incident is
open: one JSON line per tool call attempted and completed. This script adds
the records a hook cannot make, checks the log, and commits it to the site's
private audit location when the incident is over.

Commands:
  record   add an explicit record: an approval, a denial, a decision, or (for
           agents that have no hook) a command that was run
  show     where the log is and what it holds
  verify   check the hash chain and report attempts that never completed.
           The chain shows an edit or deletion anywhere but the last line; the
           'head' hash, written into the commit message, pins the last line
           and the record count too
  commit   copy the log into the audit repository on a branch, push it, open a
           pull request, and stop capture for this incident

Exit codes: 0 ok, 1 verify found a broken chain, 2 the site, the log or git
could not be used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import audit_hook
import redact
import sre_common
from sre_common import SiteError, guarded, load_site

KINDS = ("approval", "denial", "decision", "command", "note")


def read_log(incident_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every line of an incident's spooled log, raw and parsed."""
    path = audit_hook.spool_path(incident_id)
    if not path.is_file():
        raise SiteError(f"no audit log for '{incident_id}' at {path}")
    return [(line, json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify(entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Check the chain and summarise what the log holds."""
    broken = []
    previous = audit_hook.GENESIS
    for number, (line, record) in enumerate(entries, start=1):
        if record.get("prev") != previous:
            broken.append(number)
        previous = hashlib.sha256(line.encode()).hexdigest()

    pending: dict[tuple[Any, Any, str], list[dict[str, Any]]] = {}
    for _line, record in entries:
        if record.get("capture") != "hook" or record.get("event") not in ("attempt", "complete"):
            continue
        key = (record.get("session"), record.get("tool"), json.dumps(record.get("input"), sort_keys=True))
        if record["event"] == "attempt":
            pending.setdefault(key, []).append(record)
        elif pending.get(key):
            pending[key].pop(0)
    unfinished = sorted((r for records in pending.values() for r in records), key=lambda r: r["ts"])

    events: dict[str, int] = {}
    for _line, record in entries:
        events[record.get("event", "unknown")] = events.get(record.get("event", "unknown"), 0) + 1
    return {
        "records": len(entries),
        "head": previous,
        "chain_intact": not broken,
        "broken_at_lines": broken,
        "events": dict(sorted(events.items())),
        "self_reported": sum(1 for _line, record in entries if record.get("capture") == "self-reported"),
        "capture_errors": events.get("capture_error", 0),
        "attempts_without_completion": [
            {"ts": record["ts"], "tool": record.get("tool"), "input": record.get("input")} for record in unfinished
        ],
    }


def cmd_record(args: argparse.Namespace, state: dict[str, Any], _doc: dict[str, Any], _root: Path) -> int:
    if audit_hook.is_closed(state["id"]):
        raise SiteError(f"the audit log for {state['id']} is committed and closed")
    env_values = redact.env_secrets(extra_names=state.get("redact_env", []))
    record: dict[str, Any] = {
        "v": 1,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "incident": state["id"],
        "capture": "self-reported",
        "event": args.kind,
        "text": redact.redact_text(args.text, env_values),
    }
    for key in ("command", "approver"):
        if getattr(args, key):
            record[key] = redact.redact_text(getattr(args, key), env_values)
    audit_hook.append(state["id"], record)
    print(f"{state['id']}: recorded {args.kind}")
    return 0


def cmd_show(_args: argparse.Namespace, state: dict[str, Any], _doc: dict[str, Any], _root: Path) -> int:
    summary = verify(read_log(state["id"]))
    summary.pop("attempts_without_completion")
    print(json.dumps({"path": str(audit_hook.spool_path(state["id"])), **summary}, indent=2))
    return 0


def cmd_verify(_args: argparse.Namespace, state: dict[str, Any], _doc: dict[str, Any], _root: Path) -> int:
    result = verify(read_log(state["id"]))
    print(json.dumps(result, indent=2))
    return 0 if result["chain_intact"] else 1


def _git(cwd: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        raise SiteError("git is not installed")
    done = subprocess.run([git, "-C", str(cwd), *args], capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        lines = (done.stderr or done.stdout).strip().splitlines()
        raise SiteError(f"git {args[0]} failed: {lines[-1] if lines else 'no output'}")
    return done.stdout.strip()


def audit_checkout(doc: dict[str, Any], site_root: Path) -> Path:
    """Local checkout of the repository that holds audit logs."""
    alias = doc["audit"]["repo"]
    if alias == "catalog":
        return site_root
    local = doc["repos"][alias].get("path")
    if not local or not Path(local).expanduser().is_dir():
        raise SiteError(f"audit repository '{alias}' has no local checkout to commit into")
    return Path(local).expanduser()


def release_capture(incident_id: str) -> None:
    """Stop capture for this incident; drop the 'active' marker when no other log is still open."""
    audit_dir = audit_hook.state_dir() / "audit"
    (audit_dir / f"{incident_id}.closed").write_text("committed\n", encoding="utf-8")
    still_open = [p for p in audit_dir.glob("*.jsonl") if not p.with_suffix(".closed").exists()]
    if not still_open:
        (audit_hook.state_dir() / "active").unlink(missing_ok=True)


def cmd_commit(args: argparse.Namespace, state: dict[str, Any], doc: dict[str, Any], site_root: Path) -> int:
    incident_id = state["id"]
    entries = read_log(incident_id)
    result = verify(entries)
    if not result["chain_intact"]:
        raise SiteError(f"the hash chain is broken at line(s) {result['broken_at_lines']}; not committing")

    checkout = audit_checkout(doc, site_root)
    remote_head = (
        _git(checkout, "rev-parse", "--abbrev-ref", "origin/HEAD") if _has_origin_head(checkout) else "origin/main"
    )
    branch = f"audit/{incident_id}"
    _git(checkout, "fetch", "--quiet", "origin")
    target = Path(doc["audit"]["path"]) / f"{incident_id}.jsonl"
    with tempfile.TemporaryDirectory(prefix="sre-harness-audit-") as scratch:
        tree = Path(scratch) / "tree"
        _git(checkout, "worktree", "add", "--quiet", "-b", branch, str(tree), remote_head)
        try:
            (tree / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audit_hook.spool_path(incident_id), tree / target)
            _git(tree, "add", str(target))
            message = f"audit: {incident_id} ({result['records']} records)\n\nhead: {result['head']}"
            _git(tree, "commit", "--quiet", "-m", message)
            _git(tree, "push", "--quiet", "-u", "origin", branch)
        finally:
            _git(checkout, "worktree", "remove", "--force", str(tree))

    url = ""
    if not args.no_pr:
        url = sre_common.run_gh(
            [
                "pr", "create", "--repo", doc["repos"][doc["audit"]["repo"]]["repo"], "--head", branch,
                "--title", f"audit: {incident_id}",
                "--body", f"Audit log for incident {incident_id} ({state.get('url', '')}). "
                f"{result['records']} records; hash chain intact; head {result['head']}; "
                f"{len(result['attempts_without_completion'])} attempt(s) without completion.",
            ]
        ).strip()  # fmt: skip
    release_capture(incident_id)
    print(json.dumps({"incident": incident_id, "branch": branch, "file": str(target), "pull_request": url}))
    return 0


def _has_origin_head(checkout: Path) -> bool:
    try:
        _git(checkout, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD")
    except SiteError:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--incident", help="<repo-alias>-<number>; default is the current incident")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="add an explicit record")
    record.add_argument("--kind", choices=KINDS, required=True)
    record.add_argument("--text", required=True, help="what was decided, approved or done, and why")
    record.add_argument("--command", dest="command_text", help="the exact command this record is about")
    record.add_argument("--approver", help="who approved or denied")
    record.set_defaults(run=cmd_record)

    sub.add_parser("show", help="where the log is and what it holds").set_defaults(run=cmd_show)
    sub.add_parser("verify", help="check the hash chain; list attempts that never completed").set_defaults(
        run=cmd_verify
    )

    commit = sub.add_parser("commit", help="commit the log to the audit repository and stop capture")
    commit.add_argument("--no-pr", action="store_true", help="push the branch but do not open a pull request")
    commit.set_defaults(run=cmd_commit)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)
    if hasattr(args, "command_text"):
        args.command = args.command_text

    def action() -> int:
        site_root, doc = load_site(args.site)
        return args.run(args, sre_common.read_incident_state(args.incident), doc, site_root)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
