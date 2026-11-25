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
import contextlib
import hashlib
import json
import re
import shlex
import shutil
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import audit_hook
import image_files
import redact
import shell_mutations
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
    agents: dict[str, int] = {}
    for _line, record in entries:
        events[record.get("event", "unknown")] = events.get(record.get("event", "unknown"), 0) + 1
        if record.get("agent"):
            agents[record["agent"]] = agents.get(record["agent"], 0) + 1
    return {
        "records": len(entries),
        "head": previous,
        "chain_intact": not broken,
        "broken_at_lines": broken,
        "events": dict(sorted(events.items())),
        "agents": dict(sorted(agents.items())),
        "self_reported": sum(1 for _line, record in entries if record.get("capture") == "self-reported"),
        "capture_errors": events.get("capture_error", 0),
        "attempts_without_completion": [
            {"ts": record["ts"], "tool": record.get("tool"), "input": record.get("input")} for record in unfinished
        ],
        "approvals": events.get("approval", 0),
        "tier2_claims": events.get("tier2", 0),
        "unauthorised_mutations": unauthorised_mutations(entries),
        "mutation_detection": (
            "per simple command, by its program after wrappers, through carriers (ssh, sh -c, sudo, docker and "
            "kubectl exec), heredocs fed to a shell and substitutions; approvals pair on the command's words; a "
            "command too long or too malformed to parse is judged by its whole text; file edits made with other "
            "tools are not classified"
        ),
        "rebinds": events.get("rebind", 0),
        "state_dir_access": state_dir_access(entries),
        "leftover_secrets": leftover_secrets(entries),
    }


# The classifier lives in shell_mutations; these names are kept for the other scripts and the tests.
MUTATING = shell_mutations.MUTATING
HARNESS_SCRIPTS = shell_mutations.HARNESS_SCRIPTS
SCRIPTS_DIR = shell_mutations.SCRIPTS_DIR
SUBSTITUTION = shell_mutations.SUBSTITUTION
MAX_CLASSIFY = shell_mutations.MAX_CLASSIFY
is_mutation = shell_mutations.is_mutation
_is_harness_call = shell_mutations.is_harness_call


def command_segments(command: str) -> list[list[str]] | None:
    """The simple commands in a shell line, split at unquoted operators; None if it cannot be parsed.

    The plain split the chat tool policy matches its rules against; mutation detection uses
    shell_mutations, which also reads newlines, heredocs, substitutions and carriers.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # bash starts a comment only at the start of a word; shlex would end the line at any "#" and hide what follows.
    lexer.commenters = ""
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


def _readings(command: str, argv: Any = None) -> set[tuple[str, ...]]:
    """The argv an approval's command text can mean: as shell words, as plain words, and as recorded."""
    found = {tuple(command.split())}
    with contextlib.suppress(ValueError):
        found.add(tuple(shlex.split(command)))
    if isinstance(argv, list) and all(isinstance(word, str) for word in argv):
        found.add(tuple(argv))
    return {reading for reading in found if reading}


@dataclass
class _Approval:
    command: str
    readings: set[tuple[str, ...]]
    used: bool = False


class _Credit:
    """Approvals not yet spent, in the order they were recorded."""

    def __init__(self) -> None:
        self.approvals: list[_Approval] = []

    def add(self, record: dict[str, Any]) -> None:
        command = str(record["command"])
        self.approvals.append(_Approval(command, _readings(command, record.get("argv"))))

    def spend_whole(self, command: str) -> bool:
        """Spend an approval written for this exact command line."""
        return self._spend(lambda approval: approval.command == command)

    def spend(self, candidates: set[tuple[str, ...]]) -> bool:
        """Spend an approval for any of these simple commands."""
        return self._spend(lambda approval: bool(approval.readings & candidates))

    def _spend(self, matches: Any) -> bool:
        for approval in self.approvals:
            if not approval.used and matches(approval):
                approval.used = True
                return True
        return False


def _call_windows(entries: list[tuple[str, dict[str, Any]]]) -> dict[int, list[int]]:
    """For each hook-captured Bash attempt, the self-reported approvals logged before it completed.

    The hook writes a call's attempt before the call runs and its completion after, so an
    approval that audit.py recorded from inside that call sits between the two.
    """
    open_calls: dict[tuple[Any, ...], list[int]] = {}
    inside: dict[int, list[int]] = {}
    finished: dict[int, list[int]] = {}
    for index, (_line, record) in enumerate(entries):
        event = record.get("event")
        if record.get("capture") == "hook" and record.get("tool") == "Bash" and event in ("attempt", "complete"):
            key = (record.get("session"), json.dumps(record.get("input"), sort_keys=True))
            if event == "attempt":
                open_calls.setdefault(key, []).append(index)
                inside[index] = []
            elif open_calls.get(key):
                attempt = open_calls[key].pop(0)
                finished[attempt] = inside.pop(attempt)
        elif event == "approval" and record.get("via") != "chat" and record.get("command"):
            for approvals in inside.values():
                approvals.append(index)
    # A call that never completed has no window: an approval after its attempt cannot be shown to precede its run.
    return {attempt: approvals for attempt, approvals in finished.items() if approvals}


def unauthorised_mutations(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Mutating simple commands that ran with no recorded approval for that command.

    An approval authorises one run of its command, and must come before it: in an earlier
    call, or earlier in the same call (audit.py record, then the command). It is matched per
    simple command on its words, so shell quoting does not matter, and the words must be
    the same: an extra flag is a different command. An approval for a whole command line
    covers every simple command in it. Tier 2 actions do not appear here: autonomy.py
    executes them itself and records them, so a pre-approved command typed into a shell
    instead is, correctly, reported. Commands are compared as recorded, that is, after
    redaction on both sides.
    """
    # A chat approval names the one tool call it allowed. The hook records that call's
    # attempt before the permission check runs, so the approval can follow its attempt.
    # Credit needs the same call and the same input, both as redacted.
    approved_calls = {
        record["tool_use_id"]: json.dumps(record.get("input"), sort_keys=True)
        for _line, record in entries
        if record.get("event") == "approval" and record.get("via") == "chat" and record.get("tool_use_id")
    }
    windows = _call_windows(entries)
    claimed: set[int] = set()
    credit = _Credit()
    found = []
    for index, (_line, record) in enumerate(entries):
        event = record.get("event")
        if index in claimed or (event == "approval" and record.get("via") == "chat"):
            continue
        approved_input = approved_calls.get(record.get("tool_use_id"))
        if event == "attempt" and approved_input == json.dumps(record.get("input"), sort_keys=True):
            continue
        if event == "approval" and record.get("command"):
            credit.add(record)
            continue
        if record.get("tool") == "Bash" and event == "attempt":
            command = str((record.get("input") or {}).get("command", ""))
        elif event == "command":
            command = str(record.get("command", ""))
        else:
            continue
        window = [number for number in windows.get(index, []) if number not in claimed]
        unpaired = _unpaired(command, credit, (entries, window, claimed))
        if unpaired:
            found.append(
                {"ts": record["ts"], "command": command, "capture": record.get("capture"), "segments": unpaired}
            )
    return found


def _unpaired(
    command: str, credit: _Credit, call: tuple[list[tuple[str, dict[str, Any]]], list[int], set[int]]
) -> list[str]:
    """The mutating simple commands of one call that no approval covers, spending the approvals that cover the rest."""
    entries, window, claimed = call
    if not command.strip() or credit.spend_whole(command):
        return []
    found = None if len(command) > MAX_CLASSIFY else shell_mutations.units(command)
    if found is None:
        # Too long or unparsable: judged by its whole text, and only an approval of that text covers it.
        return [command] if is_mutation(command) else []
    local = _Credit()
    unpaired: list[str] = []
    for unit in found:
        records_approval, literal = shell_mutations.approval_call(unit.argv)
        if records_approval and window:
            # The approval this segment wrote: the one naming its literal command, else the next in order.
            number = next((n for n in window if entries[n][1].get("command") == literal), window[0])
            window.remove(number)
            claimed.add(number)
            local.add(entries[number][1])
            continue
        for hit in unit.hits:
            candidates = {hit, unit.argv}
            if not (local.spend(candidates) or credit.spend(candidates)):
                unpaired.append(shlex.join(hit))
    # An approval recorded in this call and not used by it stands for a later call, like any other.
    credit.approvals.extend(approval for approval in local.approvals if not approval.used)
    return unpaired


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
    """Compare the log with the head hashes pinned in the incident's timeline comments.

    A log whose first record is a genesis record written after a pin cannot hold the records that pin
    names: it is a new chain started later (a re-spool after commit, homelabia#281), not a damaged copy
    of the pinned one. That mismatch is marked 'new_chain' and says so.
    """
    mismatches = []
    checked = 0
    started = _chain_start(entries)
    for entry in timeline.fetch(state, responders):
        pin = entry.get("audit_anchor")
        if not pin:
            continue
        checked += 1
        count = pin["records"]
        if count > len(entries):
            problem = f"timeline pinned {count} records; the log has {len(entries)}"
        elif hashlib.sha256(entries[count - 1][0].encode()).hexdigest() != pin["head"]:
            problem = f"record {count} is not the one pinned in the timeline"
        else:
            continue
        if started is not None and started > sre_common.parse_utc(entry["at"]):
            problem = (
                f"a new chain started at {sre_common.iso_utc(started)} ({len(entries)} records), after the timeline "
                f"pinned {count} records at {entry['at']}: this log is not the one that was pinned"
            )
            mismatches.append({"at": entry["at"], "problem": problem, "new_chain": True})
        else:
            mismatches.append({"at": entry["at"], "problem": problem})
    return {"anchors_checked": checked, "anchor_mismatches": mismatches}


def _chain_start(entries: list[tuple[str, dict[str, Any]]]) -> datetime | None:
    """When a log's chain began: its first record's time, if that record is a genesis record."""
    if not entries or entries[0][1].get("prev") != audit_hook.GENESIS:
        return None
    try:
        return sre_common.parse_utc(str(entries[0][1].get("ts")))
    except ValueError:
        return None


def closed_message(incident_id: str) -> str:
    """Why nothing more is recorded for a committed incident, and what to do instead."""
    return (
        f"the audit log for {incident_id} is committed and closed; to record more, reopen it: "
        f"incident.py attach {incident_id} --reopen"
    )


def cmd_record(args: argparse.Namespace, state: dict[str, Any], _doc: dict[str, Any], _root: Path) -> int:
    if audit_hook.is_closed(state["id"]):
        raise SiteError(closed_message(state["id"]))
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
    if args.agent or args.model:
        # A self-reporting agent names itself; the same shape rules as the hook apply.
        record["agent"], model = audit_hook.agent_of({"agent": args.agent or "unknown", "model": args.model})
        if model:
            record["model"] = model
    if args.kind == "approval" and record.get("command"):
        with contextlib.suppress(ValueError):
            # The words the approval pairs on; verify also accepts the command split on spaces alone.
            record["argv"] = shlex.split(record["command"])
    audit_hook.append(state["id"], record)
    print(f"{state['id']}: recorded {args.kind}")
    if record.get("argv"):
        print(f"approves: {shlex.join(record['argv'])}")
    return 0


CHAT_OUTCOMES = ("approved", "denied", "expired", "refused")
# Outcomes no person gave: a request that timed out, or a call the session's policy refused without asking.
UNATTENDED_OUTCOMES = ("expired", "refused")


@dataclass(frozen=True)
class ChatDecision:
    """A chat approver's answer to one tool call. 'approver' is None only when the request expired."""

    outcome: str
    tool: str
    tool_input: dict[str, Any]
    tool_use_id: str
    approver: str | None
    link: str | None
    reason: str | None = None


def record_chat_decision(incident_id: str, decision: ChatDecision) -> dict[str, Any]:
    """Record a chat approver's decision on one tool call, before the agent is answered.

    Written by the chat gateway, not the agent. The input is redacted the way
    the hook redacts it, and also hashed exactly, so the record names the call
    it decided without storing a secret. An approval covers only the call with
    this tool_use_id.
    """
    if decision.outcome not in CHAT_OUTCOMES:
        raise ValueError(f"outcome must be one of {CHAT_OUTCOMES}")
    if decision.outcome not in UNATTENDED_OUTCOMES and not decision.approver:
        raise ValueError("an approval or denial needs the approver's identity")
    if decision.outcome == "refused" and decision.approver:
        raise ValueError("a refusal by policy names no approver")
    if audit_hook.is_closed(incident_id):
        raise SiteError(closed_message(incident_id))
    env_values = redact.env_secrets(extra_names=audit_hook.credential_names(incident_id))
    redacted = redact.redact(decision.tool_input, env_values)
    exact = json.dumps(decision.tool_input, sort_keys=True, ensure_ascii=False, default=str)
    record: dict[str, Any] = {
        "v": 1,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "incident": incident_id,
        "capture": "gateway",
        "via": "chat",
        "event": "approval" if decision.outcome == "approved" else "denial",
        "outcome": decision.outcome,
        "tool": decision.tool,
        "tool_use_id": decision.tool_use_id[:128],
        "input": redacted,
        "input_sha256": hashlib.sha256(exact.encode()).hexdigest(),
        "approver": decision.approver,
        "link": decision.link,
    }
    if decision.tool == "Bash" and isinstance(redacted.get("command"), str):
        record["command"] = redacted["command"]
    if decision.reason:
        record["reason"] = redact.redact_text(decision.reason, env_values, limit=500)
    audit_hook.append(incident_id, record)
    return record


POLICIES = ("triage-only", "chat")


@dataclass(frozen=True)
class PolicyChange:
    """A chat gateway moving one agent session from one tool policy to another, because of whose message."""

    incident: str
    session: str | None
    old: str
    new: str
    by: str
    link: str | None


def record_policy_change(change: PolicyChange) -> None:
    """Record that the gateway moved a session out of triage-only, and whose message did it.

    Written by the chat gateway. Only triage-only to chat happens: a session never goes back.
    """
    if (change.old, change.new) != POLICIES:
        raise ValueError(f"the only policy change is {POLICIES[0]} to {POLICIES[1]}; a session never goes back")
    if audit_hook.is_closed(change.incident):
        raise SiteError(closed_message(change.incident))
    audit_hook.append(
        change.incident,
        {
            "v": 1,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "incident": change.incident,
            "session": change.session,
            "capture": "gateway",
            "via": "chat",
            "event": "policy_change",
            "from": change.old,
            "to": change.new,
            "by": change.by,
            "link": change.link,
        },
    )


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


def _commit_ready(state: dict[str, Any], doc: dict[str, Any], offline: bool) -> dict[str, Any]:
    """Verify one incident's log for commit; raise if it must not be committed."""
    incident_id = state["id"]
    entries = read_log(incident_id)
    result = verify(entries)
    if not result["chain_intact"]:
        raise SiteError(
            f"{incident_id}: the hash chain is broken at line(s) {result['broken_at_lines']}; not committing"
        )
    if result["leftover_secrets"]:
        lines = ", ".join(str(item["line"]) for item in result["leftover_secrets"])
        raise SiteError(
            f"{incident_id}: line(s) {lines} of the log still look like they hold a secret; not publishing it. "
            "Run audit.py verify, show the person, and rotate anything real"
        )
    if not offline:
        pinned = check_anchors(state, entries, doc["incident"].get("responders"))
        mismatches = pinned["anchor_mismatches"]
        if mismatches and all(item.get("new_chain") for item in mismatches):
            raise SiteError(
                f"{incident_id}: {mismatches[0]['problem']}. This spool began after the pin, so it is not a damaged "
                "copy of the pinned log: that log is usually already committed on the audit repository's default "
                "branch. Not committing this spool. A person can drop it, after that log is checked against "
                "the pins, with "
                f"incident.py gc --incident {incident_id} --discard-spool, or reopen with "
                f"incident.py attach {incident_id} --reopen to continue the committed log"
            )
        if mismatches:
            raise SiteError(
                f"{incident_id}: the log does not match {len(pinned['anchor_mismatches'])} head hash(es) pinned in "
                "the timeline; not committing. Run audit.py verify --check-anchors"
            )
    return result


def cmd_commit(args: argparse.Namespace, state: dict[str, Any], doc: dict[str, Any], site_root: Path) -> int:
    incident_id = state["id"]
    if args.offline and sre_common.from_chat():
        # The deny rule is text matching; this is the control.
        raise SiteError("--offline skips the check against the timeline's pinned hashes and is not allowed from chat")
    if args.unresolved and sre_common.from_chat():
        raise SiteError("--unresolved commits the log of an open incident and is not allowed from chat")
    if state.get("status", "open") != "resolved" and not args.unresolved:
        # Committing stops capture, and the gateway then refuses every approval for the incident (homelabia#266).
        raise SiteError(
            f"{incident_id} is {state.get('status', 'open')}, not resolved: committing its log would end capture "
            f"and stop approvals being recorded. Run incident.py close --incident {incident_id} first"
        )
    # A promoted incident began as an unattended triage: its log is the first part of this one's, and goes with it.
    earlier = sre_common.read_incident_state(state["promoted_from"]) if state.get("promoted_from") else None
    if earlier is not None and not audit_hook.spool_path(earlier["id"]).is_file():
        earlier = None
    if earlier is not None:
        _commit_ready(earlier, doc, args.offline)
    result = _commit_ready(state, doc, args.offline)

    checkout = audit_checkout(doc, site_root)
    remote_head = (
        _git(checkout, "rev-parse", "--abbrev-ref", "origin/HEAD") if _has_origin_head(checkout) else "origin/main"
    )
    branch = f"audit/{incident_id}"
    _git(checkout, "fetch", "--quiet", "origin")
    target = Path(doc["audit"]["path"]) / f"{incident_id}.jsonl"
    spool = audit_hook.spool_path(incident_id).read_bytes()
    found = _already_committed(args.amend, checkout, (remote_head, branch, target), spool, (state, earlier))
    if found is not None:
        if found["status"] == "pending" and not args.no_pr:
            found["pull_request"] = _pull_request_or_blank(doc, branch)
        print(json.dumps({"incident": incident_id, "branch": branch, "file": str(target), **found}))
        return 0
    if _has_local_branch(checkout, branch):
        # Every earlier run leaves its local branch behind; the remote branch, if any, is the record.
        _git(checkout, "branch", "-D", branch)
    amend = args.amend and _git_show(checkout, f"origin/{branch}", target) is not None
    base = f"origin/{branch}" if amend else remote_head
    _push_log(checkout, (base, branch, target), (state, earlier), result, amend)

    url = ""
    if amend:
        # The pull request already exists and the push has happened: its link is only a courtesy.
        url = "" if args.no_pr else _pull_request_or_blank(doc, branch)
    elif not args.no_pr:
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
    if earlier is not None:
        release_capture(earlier["id"])
    release_capture(incident_id)
    status = "amended" if amend else "committed"
    print(
        json.dumps(
            {"incident": incident_id, "branch": branch, "file": str(target), "pull_request": url, "status": status}
        )
    )
    return 0


def _push_log(
    checkout: Path,
    where: tuple[str, str, Path],
    states: tuple[dict[str, Any], dict[str, Any] | None],
    result: dict[str, Any],
    amend: bool,
) -> None:
    """Commit the spool (and a promoted triage's beside it) on the audit branch, from base, and push it."""
    base, branch, target = where
    state, earlier = states
    with tempfile.TemporaryDirectory(prefix="sre-harness-audit-") as scratch:
        tree = Path(scratch) / "tree"
        _git(checkout, "worktree", "add", "--quiet", "-B" if amend else "-b", branch, str(tree), base)
        try:
            (tree / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audit_hook.spool_path(state["id"]), tree / target)
            _git(tree, "add", str(target))
            images = _add_images(tree, target, state["id"])
            message = f"audit: {state['id']} ({result['records']} records{', amended' if amend else ''})"
            message += f"\n\nhead: {result['head']}"
            if images:
                message += f"\nimages shown in chat: {images} (beside the log, named by sha256)"
            if earlier is not None:
                before = target.parent / f"{earlier['id']}.jsonl"
                shutil.copyfile(audit_hook.spool_path(earlier["id"]), tree / before)
                _git(tree, "add", str(before))
                _add_images(tree, before, earlier["id"])
                message += f"\npromoted from: {earlier['id']} (committed beside it)"
            _git(tree, "commit", "--quiet", "-m", message)
            _git(tree, "push", "--quiet", "-u", "origin", branch)
        finally:
            _git(checkout, "worktree", "remove", "--force", str(tree))


def _add_images(tree: Path, target: Path, incident_id: str) -> int:
    """Commit the images the gateway posted for this incident beside its log, as <id>.images/<sha256>.<type>.

    The record of what people were shown (ADR 0027). Only regular image files named by their own digest are
    taken; anything else in the directory is left out.
    """
    source = image_files.incident_images_dir(incident_id)
    if not source.is_dir() or source.is_symlink():
        return 0
    folder = target.parent / f"{incident_id}.images"
    count = 0
    for path in sorted(source.iterdir()):
        data = image_files.named_by_content(path)
        if data is None:
            continue
        (tree / folder).mkdir(parents=True, exist_ok=True)
        (tree / folder / path.name).write_bytes(data)
        _git(tree, "add", str(folder / path.name))
        count += 1
    return count


def _already_committed(
    amend: bool,
    checkout: Path,
    where: tuple[str, str, Path],
    spool: bytes,
    states: tuple[dict[str, Any], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """What an earlier commit already did, when there is nothing to do now; None to commit.

    The same log on the default branch is preserved; the same log on the audit branch waits for its pull
    request. A different log on the audit branch is refused unless --amend: an audit record is never
    silently replaced.
    """
    remote_head, branch, target = where
    state, earlier = states
    merged = _git_show(checkout, remote_head, target)
    if merged == spool:
        _release_all(state, earlier)
        return {"status": "preserved", "pull_request": "", "note": f"already on {remote_head}; nothing to do"}
    if merged is not None and not spool.startswith(merged):
        # An audit record on the default branch is only ever extended, never replaced (homelabia#281).
        raise SiteError(
            f"{state['id']}: {target} on {remote_head} holds {len(merged.splitlines())} records that this spool "
            f"({len(spool.splitlines())} records) does not start with; not overwriting an audit record. "
            f"A spool started after the log was committed can be dropped by a person with "
            f"incident.py gc --incident {state['id']} --discard-spool"
        )
    pushed = _git_show(checkout, f"origin/{branch}", target)
    if pushed is None:
        return None
    if pushed == spool:
        _release_all(state, earlier)
        return {"status": "pending", "pull_request": "", "note": "already committed; waiting for its pull request"}
    if amend:
        return None
    added = len(spool.splitlines()) - len(pushed.splitlines())
    raise SiteError(
        f"{state['id']}: the log changed since it was committed to origin/{branch} ({added:+d} records); "
        "not overwriting an audit record. Rerun with --amend to push the new log to the same branch"
    )


def _release_all(state: dict[str, Any], earlier: dict[str, Any] | None) -> None:
    if earlier is not None:
        release_capture(earlier["id"])
    release_capture(state["id"])


def _pull_request_or_blank(doc: dict[str, Any], branch: str) -> str:
    try:
        return _pull_request(doc, branch)
    except SiteError:
        return ""


def _pull_request(doc: dict[str, Any], branch: str) -> str:
    """The URL of the open pull request for a branch, or blank."""
    repo = doc["repos"][doc["audit"]["repo"]]["repo"]
    out = sre_common.run_gh(["pr", "list", "--repo", repo, "--head", branch, "--json", "url", "--jq", ".[0].url"])
    return out.strip()


def _git_show(cwd: Path, ref: str, path: Path) -> bytes | None:
    """A file's bytes at a ref, or None when the ref or file is not there."""
    git = shutil.which("git")
    if not git:
        raise SiteError("git is not installed")
    done = subprocess.run([git, "-C", str(cwd), "show", f"{ref}:{path}"], capture_output=True, check=False)  # noqa: S603
    return done.stdout if done.returncode == 0 else None


def _has_local_branch(checkout: Path, branch: str) -> bool:
    try:
        _git(checkout, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except SiteError:
        return False
    return True


def _has_origin_head(checkout: Path) -> bool:
    try:
        _git(checkout, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD")
    except SiteError:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    # No abbreviated options: the chat policy matches option names as text (homelabia#273).
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--incident", help="<repo-alias>-<number>; default is the current incident")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="add an explicit record")
    record.add_argument("--kind", choices=KINDS, required=True)
    record.add_argument("--text", required=True, help="what was decided, approved or done, and why")
    record.add_argument("--command", dest="command_text", help="the exact command this record is about")
    record.add_argument("--approver", help="who approved or denied")
    record.add_argument("--agent", help="the agent writing this record, if it has no hook (codex, opencode, ...)")
    record.add_argument("--model", help="the model it runs, as provider/model")
    record.set_defaults(run=cmd_record)

    sub.add_parser("show", help="where the log is and what it holds").set_defaults(run=cmd_show)
    verifier = sub.add_parser("verify", help="check the hash chain; list attempts that never completed")
    verifier.add_argument(
        "--check-anchors", action="store_true", help="also compare with head hashes pinned in the timeline (needs gh)"
    )
    verifier.set_defaults(run=cmd_verify)

    commit = sub.add_parser(
        "commit", help="commit the log to the audit repository and stop capture", allow_abbrev=False
    )
    commit.add_argument("--no-pr", action="store_true", help="push the branch but do not open a pull request")
    commit.add_argument("--offline", action="store_true", help="skip the comparison with the timeline's pinned hashes")
    commit.add_argument(
        "--amend", action="store_true", help="the log changed since it was committed: push it to the same branch"
    )
    commit.add_argument(
        "--unresolved",
        action="store_true",
        help="commit the log of an incident that is not resolved (ends capture; not allowed from chat)",
    )
    commit.set_defaults(run=cmd_commit)
    sre_common.incident_after_subcommand(sub)
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
