#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Work with the audit log of an incident.

The log is written by the audit hook (audit_hook.py) while the incident is
open: one JSON line per tool call attempted and completed. It is a record
kept by a cooperating agent, with checks that make damage and later edits
visible; it is not proof against a process on the same account that sets out
to defeat it (see audit_hook.py). This script adds
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
import re
import shlex
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
import timeline
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
        "approvals": events.get("approval", 0),
        "tier2_claims": events.get("tier2", 0),
        "unauthorised_mutations": unauthorised_mutations(entries),
        "mutation_detection": (
            "pattern-based on shell commands, per simple command; a command too long to classify counts as a "
            "mutation; file edits made with other tools are not classified"
        ),
        "rebinds": events.get("rebind", 0),
        "state_dir_access": state_dir_access(entries),
        "leftover_secrets": leftover_secrets(entries),
    }


# Commands that change a system. Deliberately broad: a false positive costs a line in a
# report, a false negative hides an unapproved change. Matched anywhere in the command, so
# a mutation wrapped in ssh, sudo or a shell pipeline is still seen.
MUTATING = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bkubectl\b.{0,400}\b(delete|apply|create|patch|replace|edit|scale|cordon|uncordon|drain|taint|label)\b",
        r"\bkubectl\b.{0,400}\b(annotate|set)\b",
        r"\bkubectl\b.{0,400}\brollout\s+(restart|undo|pause|resume)\b",
        r"\bhelm\b.{0,400}\b(install|upgrade|uninstall|rollback|delete)\b",
        r"\bsystemctl\b.{0,200}\b(start|stop|restart|reload|enable|disable|kill|mask|unmask)\b",
        r"\bservice\s+\S{1,100}\s+(start|stop|restart|reload)\b",
        r"\bdocker\b.{0,200}\b(restart|stop|start|kill|rm|rmi|prune|run|exec)\b",
        r"\b(docker\s+compose|docker-compose)\b.{0,200}\b(up|down|restart|stop|start|rm|pull)\b",
        r"\b(crictl|ctr)\b.{0,200}\b(rm|rmp|rmi|stop|stopp|kill)\b",
        r"\bterraform\b.{0,200}\b(apply|destroy|import|taint|untaint)\b|\bterraform\s+state\s+(rm|mv)\b",
        r"\bansible(-playbook)?\b",
        r"\b(reboot|shutdown|poweroff|halt)\b|\binit\s+[06]\b",
        r"\b(rm|rmdir|mv|truncate|shred|dd|mkfs\S{0,20}|fsck\S{0,20}|mount|umount|chmod|chown|ln)\s",
        r"\b(kill|pkill|killall)\b",
        r"\bgit\b.{0,200}\b(push|reset|rebase|merge|revert)\b",
        r"\b(apt|apt-get|dnf|yum|snap|pip|npm)\b.{0,200}\b(install|remove|purge|upgrade|refresh|uninstall)\b",
        r"\b(iptables|nft)\b|\bip\s+(route|link|addr)\s+(add|del|delete|replace|flush|set)\b",
        r"\bqm\s+(start|stop|shutdown|reset|reboot|destroy|set|migrate)\b",
        r"\btee\b|\bsed\s+(-\S{0,8}i|--in-place)",
    )
)
# The harness's own record-keeping changes the tracker and the audit branch; that is not a
# system change. Only a segment that is itself one plain call of a harness script is exempt.
HARNESS_SCRIPTS = frozenset(
    {"incident.py", "timeline.py", "audit.py", "autonomy.py", "catalog_sync.py", "catalog_check.py", "validate_site.py"}
)
SCRIPTS_DIR = Path(__file__).resolve().parent
SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")
MAX_CLASSIFY = 20000


def _segments(command: str) -> list[list[str]] | None:
    """Split a command into simple commands at unquoted shell operators. None if it cannot be parsed."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|<>()"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _is_harness_call(tokens: list[str]) -> bool:
    """True only for a plain call of one of this plugin's own scripts.

    Accepted forms: '<script> …', 'uv run <script> …', 'python3 <script> …'. No
    option may sit between the launcher and the script: an option could name
    other code to run. The script must be given as an absolute path to a file
    in this plugin's scripts directory, not merely a path that ends the same way.
    """
    words = list(tokens)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    elif words and re.fullmatch(r"python3?(\.\d+)?", words[0]):
        words = words[1:]
    if not words or words[0].startswith("-"):
        return False
    try:
        given = Path(words[0]).expanduser()
        # A relative path would be resolved against wherever verify happens to run, not
        # where the command ran. Only an absolute path can be trusted to name our script.
        if not given.is_absolute():
            return False
        script = given.resolve()
    except (OSError, RuntimeError):
        return False
    return script.parent == SCRIPTS_DIR and script.name in HARNESS_SCRIPTS and script.is_file()


def is_mutation(command: str) -> bool:
    """Whether a shell command looks like it changes a system.

    Fails closed: a command too long to classify, or one that cannot be parsed,
    is treated by its whole text; substitution anywhere removes any exemption.
    """
    if not command.strip():
        return False
    if len(command) > MAX_CLASSIFY:
        return True
    segments = None if SUBSTITUTION.search(command) else _segments(command)
    if segments is None:
        return any(pattern.search(command) for pattern in MUTATING)
    for tokens in segments:
        if _is_harness_call(tokens):
            continue
        if any(pattern.search(" ".join(tokens)) for pattern in MUTATING):
            return True
    return False


def unauthorised_mutations(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Mutating shell commands that ran with no recorded approval for that exact command.

    An approval authorises one run of its command, and must come before it. Tier 2
    actions do not appear here: autonomy.py executes them itself and records them, so a
    pre-approved command typed into a shell instead is, correctly, reported.
    Commands are compared as recorded, that is, after redaction on both sides.
    """
    credit: dict[str, int] = {}
    found = []
    for _line, record in entries:
        event = record.get("event")
        if event == "approval" and record.get("command"):
            credit[record["command"]] = credit.get(record["command"], 0) + 1
            continue
        if record.get("tool") == "Bash" and event == "attempt":
            command = str((record.get("input") or {}).get("command", ""))
        elif event == "command":
            command = str(record.get("command", ""))
        else:
            continue
        if not is_mutation(command):
            continue
        if credit.get(command, 0) > 0:
            credit[command] -= 1
        else:
            found.append({"ts": record["ts"], "command": command, "capture": record.get("capture")})
    return found


HARNESS_SCRIPT = re.compile(r"scripts/(?:incident|timeline|audit|autonomy)\.py\b")
STATE_ACCESS = re.compile(r"sre-harness/(?:active|audit|sessions|binds|incidents)\b|state/sre-harness\b")


def state_dir_access(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Tool calls that touched the harness's own state files, other than through its scripts.

    Those files decide whether capture happens. Nothing but the harness scripts
    has a reason to touch them during an incident.
    """
    found = []
    for _line, record in entries:
        if record.get("event") != "attempt":
            continue
        text = json.dumps(record.get("input") or {})
        if STATE_ACCESS.search(text) and not HARNESS_SCRIPT.search(text):
            found.append({"ts": record["ts"], "tool": record.get("tool"), "input": record.get("input")})
    return found


def leftover_secrets(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Lines that still contain something secret-shaped after redaction. Values are not returned."""
    found = []
    for number, (line, _record) in enumerate(entries, start=1):
        labels = redact.find_secrets_in_text(line.replace("[REDACTED:", "[R "))
        if labels:
            found.append({"line": number, "looks_like": sorted(set(labels))})
    return found


def check_anchors(
    state: dict[str, Any], entries: list[tuple[str, dict[str, Any]]], responders: list[str] | None
) -> dict[str, Any]:
    """Compare the log with the head hashes pinned in the incident's timeline comments."""
    mismatches = []
    checked = 0
    for entry in timeline.fetch(state, responders):
        pin = entry.get("audit_anchor")
        if not pin:
            continue
        checked += 1
        count = pin["records"]
        if count > len(entries):
            problem = f"timeline pinned {count} records; the log has {len(entries)}"
            mismatches.append({"at": entry["at"], "problem": problem})
        elif hashlib.sha256(entries[count - 1][0].encode()).hexdigest() != pin["head"]:
            mismatches.append({"at": entry["at"], "problem": f"record {count} is not the one pinned in the timeline"})
    return {"anchors_checked": checked, "anchor_mismatches": mismatches}


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
    for key in ("attempts_without_completion", "state_dir_access", "unauthorised_mutations", "mutation_detection"):
        summary.pop(key)
    print(json.dumps({"path": str(audit_hook.spool_path(state["id"])), **summary}, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace, state: dict[str, Any], doc: dict[str, Any], _root: Path) -> int:
    entries = read_log(state["id"])
    result = verify(entries)
    if args.check_anchors:
        result.update(check_anchors(state, entries, doc["incident"].get("responders")))
    print(json.dumps(result, indent=2))
    return 0 if result["chain_intact"] and not result.get("anchor_mismatches") else 1


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
    """Stop capture for this incident. The 'active' marker goes only when no other incident is live."""
    audit_dir = audit_hook.state_dir() / "audit"
    (audit_dir / f"{incident_id}.closed").write_text("committed\n", encoding="utf-8")
    audit_hook.release_active_marker()


def cmd_commit(args: argparse.Namespace, state: dict[str, Any], doc: dict[str, Any], site_root: Path) -> int:
    incident_id = state["id"]
    entries = read_log(incident_id)
    result = verify(entries)
    if not result["chain_intact"]:
        raise SiteError(f"the hash chain is broken at line(s) {result['broken_at_lines']}; not committing")
    if result["leftover_secrets"]:
        lines = ", ".join(str(item["line"]) for item in result["leftover_secrets"])
        raise SiteError(
            f"line(s) {lines} of the log still look like they hold a secret; not publishing it. "
            "Run audit.py verify, show the person, and rotate anything real"
        )
    if not args.offline:
        pinned = check_anchors(state, entries, doc["incident"].get("responders"))
        if pinned["anchor_mismatches"]:
            raise SiteError(
                f"the log does not match {len(pinned['anchor_mismatches'])} head hash(es) pinned in the timeline; "
                "not committing. Run audit.py verify --check-anchors"
            )

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
                f"{len(result['attempts_without_completion'])} attempt(s) without completion; "
                f"{len(result['unauthorised_mutations'])} mutation(s) with no approval or Tier 2 claim.",
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
    verifier = sub.add_parser("verify", help="check the hash chain; list attempts that never completed")
    verifier.add_argument(
        "--check-anchors", action="store_true", help="also compare with head hashes pinned in the timeline (needs gh)"
    )
    verifier.set_defaults(run=cmd_verify)

    commit = sub.add_parser("commit", help="commit the log to the audit repository and stop capture")
    commit.add_argument("--no-pr", action="store_true", help="push the branch but do not open a pull request")
    commit.add_argument("--offline", action="store_true", help="skip the comparison with the timeline's pinned hashes")
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
