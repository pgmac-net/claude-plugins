#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Open, update and close the record of an incident.

An incident is tracked by one issue. Its body holds the stable facts, edited
in place as they firm up; the timeline is its comments (see timeline.py).
The incident id is '<repo-alias>-<issue number>', for example 'incidents-97'.

Commands:
  open     create the tracking issue (refuses if open incidents exist, unless --new)
  attach   adopt an existing tracking issue as the current incident
  status   show the current incident
  set      change severity, phase, affected scope, services, impact or runbook in the issue body
  close    mark the incident resolved locally and in the body; the issue stays open
  promote  make an unattended triage (open --auto) a real incident on the site's tracker
  dismiss  close an unattended triage judged not to be an incident
  gc       clear local state of incidents whose audit log has merged (dry run unless --apply;
           --incident <id>, repeatable, clears only those)

Exit codes: 0 ok, 2 the site or the code host could not be used, 3 open
incidents already exist.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import audit_hook
import sre_common
import state_gc
from sre_common import SEVERITIES, SiteError, guarded, iso_utc, load_site, local_clock, split_incident_id, utc_now

MARKER = "<!-- sre-harness:incident v1 -->"
PHASES = ("Establishing picture", "Diagnosing", "Mitigating", "Verifying", "Resolved")
LABEL_COLOUR = "b60205"
AUTO_LABEL = "auto-triage"
DISMISSED_LABEL = "not-an-incident"
# Timeline entries a promotion carries to the public record: curated conclusions, not raw evidence.
CARRIED_TYPES = ("decision", "summary")
FIELDS = {"severity": "Severity", "phase": "Phase", "affected": "Affected", "services": "Services"}
# Lines added after the first release of the body format, and the line each goes after when missing.
LATER_FIELDS = {"Services": "Affected", "Resolved": "Services"}
NOT_KNOWN = "not yet known"
SECTIONS = {"impact": "Impact", "runbook": "Runbook"}

BODY = """{marker}
## Incident

**Detected:** {clock} ({utc})
**Severity:** {severity} (provisional)
**Phase:** {phase}
**Affected:** {affected}
**Services:** {services}

## Impact

{impact}

## Runbook

None matched yet.

---
*Stable facts live in this body and are edited in place. The timeline is the comments below, oldest first.*
"""


def tracker(doc: dict[str, Any], drill: bool, auto: bool = False) -> tuple[str, str, str]:
    """Return (repo alias, code-host repository, label) for real, practice or unattended incidents.

    An unattended triage's record goes to runner.tracker_repo, a private repository,
    labelled only auto-triage: the site's incident label marks action items there.
    """
    if drill:
        settings = doc["incident"].get("drill")
        if not settings:
            raise SiteError("site.yaml has no incident.drill section; cannot open a drill")
        alias, label = settings["repo"], settings["label"]
    elif auto:
        runner = doc.get("runner") or {}
        if not runner.get("tracker_repo"):
            raise SiteError("site.yaml has no runner.tracker_repo; cannot open an unattended triage")
        alias, label = runner["tracker_repo"], AUTO_LABEL
    else:
        alias, label = doc["tracker"]["repo"], doc["tracker"]["incident_label"]
    return alias, doc["repos"][alias]["repo"], label


def open_incidents(repo: str, label: str, also: str | None = None) -> list[dict[str, Any]]:
    """Open tracking issues carrying the label (and the second label, if given)."""
    labels = ["--label", label] + (["--label", also] if also else [])
    out = sre_common.run_gh(["issue", "list", "--repo", repo, *labels, "--state", "open", "--json", "number,title,url"])
    return json.loads(out or "[]")


LABEL_DESCRIPTIONS = {
    "drill": "Practice incident, not a real one",
    "auto": "Unattended triage of an alert; not yet reviewed by a person",
    "dismissed": "Triaged, and judged not to be an incident",
    "incident": "Work arising from a production incident",
}


def ensure_label(repo: str, label: str, kind: str) -> None:
    """Create a label if the repository lacks it. kind is a key of LABEL_DESCRIPTIONS."""
    try:
        sre_common.run_gh(
            [
                "label",
                "create",
                label,
                "--repo",
                repo,
                "--color",
                LABEL_COLOUR,
                "--description",
                LABEL_DESCRIPTIONS[kind],
            ]
        )
    except SiteError as exc:
        if "already exists" not in str(exc):
            raise


def _field_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"^\*\*{re.escape(name)}:\*\* .*$", re.MULTILINE)


def set_field(body: str, name: str, value: str) -> str:
    """Replace one '**Name:** value' line in the issue body.

    A line added to the format later is inserted when an older body lacks it.
    """
    pattern = _field_pattern(name)
    if not pattern.search(body):
        if name not in LATER_FIELDS:
            raise SiteError(f"issue body has no '**{name}:**' line; it was not opened by incident.py")
        anchor = LATER_FIELDS[name]
        if not _field_pattern(anchor).search(body):
            body = set_field(body, anchor, NOT_KNOWN)
        return _field_pattern(anchor).sub(lambda match: f"{match.group(0)}\n**{name}:** {value}", body, count=1)
    return pattern.sub(lambda _match: f"**{name}:** {value}", body, count=1)


def read_field(body: str, name: str) -> str | None:
    """The value of one '**Name:** value' line, or None."""
    found = _field_pattern(name).search(body or "")
    return found.group(0).split(":** ", 1)[1].strip() if found else None


def resolved_at_from_body(body: str | None) -> str | None:
    """The UTC time in a body's '**Resolved:** 20:08 AEST (2026-11-19T10:08:12Z)' line, or None."""
    value = read_field(body or "", "Resolved")
    found = re.search(r"\((\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\)", value or "")
    return found.group(1) if found else None


def known_services(site_root: Path, doc: dict[str, Any]) -> set[str]:
    """Component refs in the site's catalog."""
    import catalog_check  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    report = catalog_check.CatalogReport()
    entities = catalog_check.load_entities(site_root / doc["catalog"]["path"], report)
    return {entity.ref for entity in entities if entity.doc["kind"] == "Component"}


def services_value(text: str, known: set[str]) -> str:
    """Normalise '--services a,component:b' to 'component:a, component:b', refusing names not in the catalog."""
    refs = []
    for part in (item.strip() for item in text.split(",")):
        if not part:
            continue
        ref = part if part.startswith("component:") else f"component:{part}"
        if ref not in known:
            raise SiteError(f"{ref} is not a service in the catalog; services are catalog Components")
        if ref not in refs:
            refs.append(ref)
    if not refs:
        raise SiteError("--services needs at least one catalog service")
    return ", ".join(refs)


def parse_services(body: str) -> list[str]:
    """Component refs from a body's '**Services:**' line; empty when absent or not yet known."""
    value = read_field(body, "Services")
    if not value or value == NOT_KNOWN:
        return []
    return [part.strip() for part in value.split(",") if part.strip().startswith("component:")]


def set_section(body: str, heading: str, text: str) -> str:
    """Replace the text under one '## Heading' in the issue body."""
    pattern = re.compile(rf"(^## {re.escape(heading)}\n\n)(.*?)(?=\n## |\n---\n|\Z)", re.MULTILINE | re.DOTALL)
    if not pattern.search(body):
        raise SiteError(f"issue body has no '## {heading}' section; it was not opened by incident.py")
    return pattern.sub(lambda match: f"{match.group(1)}{text.strip()}\n", body, count=1)


def start_capture(state: dict[str, Any], doc: dict[str, Any]) -> None:
    """Tell the audit hook an incident is live, which environment variables hold credentials, and issue a bind token."""
    names = {name for source in doc.get("monitoring", []) for name in source.get("credentials_env", [])}
    assist = doc["incident"].get("assist") or {}
    if assist.get("api_key_env"):
        names.add(assist["api_key_env"])
    state["redact_env"] = sorted(names)
    marker = sre_common.state_dir() / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("an incident is open; the audit hook is capturing\n", encoding="utf-8")
    # A random single-use token. The hook binds the session that prints it, and reads the
    # incident id from this file, never from tool output.
    token = secrets.token_hex(16)
    binds = sre_common.state_dir() / "binds"
    binds.mkdir(parents=True, exist_ok=True)
    (binds / token).write_text(state["id"] + "\n", encoding="utf-8")
    state["bind"] = token


def already_open(
    doc: dict[str, Any], args: argparse.Namespace, where: tuple[str, str, str]
) -> list[tuple[str, dict[str, Any]]]:
    """(alias, issue) for each open incident an open should know about first.

    An unattended triage also lists people's incidents on the site's tracker,
    so it can say when it looks like one already being worked.
    """
    alias, repo, label = where
    found = [(alias, issue) for issue in open_incidents(repo, label)]
    if args.auto and not args.drill:
        pub_alias, pub_repo, pub_label = tracker(doc, False)
        found += [(pub_alias, issue) for issue in open_incidents(pub_repo, pub_label)]
    return found


def cmd_open(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    cleared = state_gc.auto_clean(doc, args.site_root)
    if cleared:
        print(f"Cleared local state of {cleared} earlier incident(s) whose audit log has merged.", file=sys.stderr)
    if sre_common.triage_token() and not args.auto:
        # The gateway's rules allow only 'open --auto' in a triage, but an agent outside the gateway
        # (or a model that ignores its instructions) would otherwise open on the public tracker.
        raise SiteError("this session is an unattended triage; open its record with --auto (it stays private)")
    alias, repo, label = tracker(doc, args.drill, args.auto)
    existing = already_open(doc, args, (alias, repo, label))
    if existing and not args.new:
        hint = (
            "An unattended triage cannot attach: pass --new and name the related one in a discovery entry"
            if args.auto
            else "Attach to one, or pass --new if this is separate"
        )
        print(f"{len(existing)} open incident(s). {hint}:")
        for owner, issue in existing:
            print(f"  {owner}-{issue['number']}  {issue['title']}  {issue['url']}")
        return 3

    now = utc_now()
    body = BODY.format(
        marker=MARKER,
        clock=local_clock(now, doc["incident"]["timezone"]),
        utc=iso_utc(now),
        severity=args.severity,
        phase=PHASES[0],
        affected=args.affected or NOT_KNOWN,
        services=services_value(args.services, known_services(args.site_root, doc)) if args.services else NOT_KNOWN,
        impact=args.impact or "Not yet known.",
    )
    labels = [label]
    ensure_label(repo, label, "drill" if args.drill else "auto" if args.auto else "incident")
    if args.auto and args.drill:
        ensure_label(repo, AUTO_LABEL, "auto")
        labels.append(AUTO_LABEL)
    title = f"[DRILL] {args.title}" if args.drill else args.title
    url = sre_common.run_gh(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            *(f"--label={name}" for name in labels),
            "--body-file",
            "-",
        ],
        stdin=body,
    ).strip()
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    state = {
        "id": f"{alias}-{number}",
        "repo": repo,
        "number": number,
        "url": url,
        "title": title,
        "drill": args.drill,
        "auto": args.auto,
        # Ties the record to the triage session that opened it: only that session may write to it.
        **({"triage_token": sre_common.triage_token()} if args.auto and sre_common.triage_token() else {}),
        "severity": args.severity,
        "phase": PHASES[0],
        "opened_at": iso_utc(now),
        "status": "open",
    }
    start_capture(state, doc)
    token = state.pop("bind")
    sre_common.write_incident_state(state)
    print(json.dumps({"id": state["id"], "url": url, "bind": token}))
    return 0


def cmd_attach(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    repo, number = split_incident_id(args.incident, doc)
    issue = json.loads(
        sre_common.run_gh(["issue", "view", str(number), "--repo", repo, "--json", "title,url,body,createdAt,labels"])
    )
    severity = re.search(r"^\*\*Severity:\*\* (P[1-4])", issue["body"] or "", re.MULTILINE)
    phase = re.search(r"^\*\*Phase:\*\* (.+)$", issue["body"] or "", re.MULTILINE)
    drill = doc["incident"].get("drill", {})
    from_issue = {
        "id": args.incident,
        "repo": repo,
        "number": number,
        "url": issue["url"],
        "title": issue["title"],
        "drill": any(label["name"] == drill.get("label") for label in issue.get("labels", [])),
        "severity": severity.group(1) if severity else None,
        "phase": phase.group(1).strip() if phase else None,
    }
    path = sre_common.incident_state_path(args.incident)
    known = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    # What the issue says is refreshed; what only local state knows (status, resolution, triage, promotion) is kept.
    state = {**(known or {"opened_at": iso_utc(sre_common.parse_utc(issue["createdAt"])), "status": "open"})}
    state.update(from_issue)
    if known is None and from_issue["phase"] == "Resolved":
        # No local state to keep (cleared by gc, or another host): the body is all that says it was resolved.
        state["status"] = "resolved"
        stamp = resolved_at_from_body(issue["body"])
        if stamp:
            state["resolved_at"] = stamp
        else:
            print(f"{args.incident} is resolved but its body has no Resolved time; none recorded.", file=sys.stderr)
    resolved = state.get("status") == "resolved"
    if resolved and args.reopen:
        sre_common.check_triage_write(state)
        state["status"] = "open"
        state.pop("resolved_at", None)
    # A resolved incident whose audit log is committed has nothing left to capture into: a bind token or an
    # 'active' marker would only keep the hook switched on for an incident that cannot take more records.
    token = None
    if args.reopen or not resolved or not audit_hook.is_closed(args.incident):
        start_capture(state, doc)
        token = state.pop("bind")
    sre_common.write_incident_state(state)
    if resolved and args.reopen:
        import timeline  # noqa: PLC0415  pylint: disable=import-outside-toplevel

        timeline.post(state, "decision", "Reopened (incident.py attach --reopen).", doc["incident"]["timezone"], None)
    print(json.dumps({"id": state["id"], "url": state["url"], "bind": token}))
    if resolved and not args.reopen:
        print(
            f"{state['id']} is resolved; attached for follow-up, still resolved. Pass --reopen to reopen it.",
            file=sys.stderr,
        )
    return 0


def cmd_status(args: argparse.Namespace, _doc: dict[str, Any]) -> int:
    print(json.dumps(sre_common.read_incident_state(args.incident), indent=2))
    return 0


def apply_changes(state: dict[str, Any], changes: dict[str, str], final: bool = False) -> None:
    """Edit the issue body in place and mirror severity and phase into local state."""
    sre_common.check_triage_write(state)
    body = json.loads(
        sre_common.run_gh(["issue", "view", str(state["number"]), "--repo", state["repo"], "--json", "body"])
    )["body"]
    for key, value in changes.items():
        if key == "severity":
            body = set_field(body, "Severity", f"{value} ({'final' if final else 'provisional'})")
        elif key in FIELDS:
            body = set_field(body, FIELDS[key], value)
        elif key == "resolved":
            body = set_field(body, "Resolved", value)
        else:
            body = set_section(body, SECTIONS[key], value)
    sre_common.run_gh(["issue", "edit", str(state["number"]), "--repo", state["repo"], "--body-file", "-"], stdin=body)
    state.update({key: changes[key] for key in ("severity", "phase") if key in changes})


def cmd_set(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    state = sre_common.read_incident_state(args.incident)
    changes = {key: getattr(args, key) for key in (*FIELDS, *SECTIONS) if getattr(args, key) is not None}
    if not changes:
        raise SiteError(
            "nothing to change; pass at least one of --severity, --phase, --affected, --services, --impact, --runbook"
        )
    if "services" in changes:
        changes["services"] = services_value(changes["services"], known_services(args.site_root, doc))
    apply_changes(state, changes, args.final)
    sre_common.write_incident_state(state, make_current=False)
    print(f"{state['id']}: updated {', '.join(changes)}")
    return 0


def cmd_close(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    state = sre_common.read_incident_state(args.incident)
    now = utc_now()
    apply_changes(
        state, {"phase": "Resolved", "resolved": f"{local_clock(now, doc['incident']['timezone'])} ({iso_utc(now)})"}
    )
    state.update({"status": "resolved", "resolved_at": iso_utc(now)})
    sre_common.write_incident_state(state, make_current=False)
    print(f"{state['id']}: resolved. The tracking issue stays open until a person closes it.")
    print_close_out(state, doc, args.site_root)
    return 0


def close_out_steps(state: dict[str, Any], doc: dict[str, Any], site_root: Path) -> list[str]:
    """The close-out steps still to do for a resolved incident, worked out from the real state, in order."""
    incident_id = state["id"]
    steps = []
    if audit_hook.spool_path(incident_id).is_file():
        if not audit_hook.is_closed(incident_id):
            steps.append(f"Commit the audit log: audit.py commit --incident {incident_id}")
            steps.append("A person merges the audit log's pull request")
        else:
            # Committed: is it on the default branch yet? Fetch only now, when the answer can be yes.
            why = state_gc.preserved(incident_id, doc, site_root, set(), state_gc.Policy())
            if why:
                steps.append(f"A person merges the audit log's pull request ({why})")
    try:
        issue = state_gc.issue_state(incident_id, doc)
    except SiteError as exc:
        steps.append(f"A person closes the tracking issue, if still open (could not check: {exc})")
    else:
        if issue != "CLOSED":
            steps.append(f"A person closes the tracking issue: {state.get('url', incident_id)}")
    steps.append("Clear local state: incident.py gc --apply")
    return steps


def print_close_out(state: dict[str, Any], doc: dict[str, Any], site_root: Path) -> None:
    print(f"Close-out for {state['id']}, still to do:")
    for number, step in enumerate(close_out_steps(state, doc, site_root), 1):
        print(f"  {number}. {step}")


def _auto_state(incident_id: str | None) -> dict[str, Any]:
    state = sre_common.read_incident_state(incident_id)
    if not state.get("auto"):
        raise SiteError(f"{state['id']} is not an unattended triage; only those are promoted or dismissed")
    if state.get("promoted_to"):
        raise SiteError(f"{state['id']} was already promoted to {state['promoted_to']}")
    return state


def cmd_promote(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    """Make an unattended triage a real incident on the site's tracker, at a responder's word.

    The public issue gets the triage's body and its decision and summary entries,
    not its raw evidence. The private issue is closed with a link. The session is
    rebound to the new incident, whose audit log starts with a record that names
    the triage log's head, so the two logs chain; audit.py commit commits both.
    """
    import timeline  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    private = _auto_state(args.incident)
    if private.get("drill"):
        raise SiteError("a drill is never promoted to the public tracker; it stays a drill")
    body = json.loads(
        sre_common.run_gh(["issue", "view", str(private["number"]), "--repo", private["repo"], "--json", "body"])
    )["body"]
    carried = [entry for entry in timeline.fetch(private) if entry["type"] in CARRIED_TYPES]
    alias, repo, label = tracker(doc, False)
    ensure_label(repo, label, "incident")
    note = (
        f"\n*Opened by unattended triage; promoted by {args.by}. "
        f"The triage record is kept privately as {private['id']}.*\n"
    )
    url = sre_common.run_gh(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            args.title or private["title"],
            "--label",
            label,
            "--body-file",
            "-",
        ],
        stdin=body.rstrip("\n") + "\n" + note,
    ).strip()
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    state = {
        **{key: private[key] for key in ("severity", "phase", "opened_at") if key in private},
        "id": f"{alias}-{number}",
        "repo": repo,
        "number": number,
        "url": url,
        "title": args.title or private["title"],
        "drill": False,
        "status": "open",
        "promoted_from": private["id"],
    }
    start_capture(state, doc)
    token = state.pop("bind")
    sre_common.write_incident_state(state)
    head = audit_hook.head(private["id"])
    audit_hook.self_report(
        state["id"],
        "promoted_from",
        source=private["id"],
        source_records=head[0] if head else 0,
        source_head=head[1] if head else None,
        by=args.by,
    )
    timezone = doc["incident"]["timezone"]
    for entry in carried:
        timeline.post(state, entry["type"], f"(from triage) {entry['text']}", timezone, entry["at"])
    timeline.post(state, "decision", f"Promoted from unattended triage {private['id']} by {args.by}.", timezone, None)
    timeline.post(private, "decision", f"Promoted to {state['id']} by {args.by}.", timezone, None)
    sre_common.run_gh(["issue", "close", str(private["number"]), "--repo", private["repo"], "--reason", "completed"])
    now = utc_now()
    private.update({"status": "resolved", "resolved_at": iso_utc(now), "promoted_to": state["id"]})
    sre_common.write_incident_state(private, make_current=False)
    print(json.dumps({"id": state["id"], "url": url, "bind": token}))
    return 0


def cmd_dismiss(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    """Close an unattended triage that a responder judged not to be an incident."""
    import timeline  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    state = _auto_state(args.incident)
    timeline.post(
        state, "decision", f"Not an incident, per {args.by}: {args.reason}", doc["incident"]["timezone"], None
    )
    ensure_label(state["repo"], DISMISSED_LABEL, "dismissed")
    sre_common.run_gh(["issue", "edit", str(state["number"]), "--repo", state["repo"], "--add-label", DISMISSED_LABEL])
    sre_common.run_gh(["issue", "close", str(state["number"]), "--repo", state["repo"], "--reason", "not planned"])
    now = utc_now()
    state.update({"status": "resolved", "resolved_at": iso_utc(now), "dismissed": True})
    sre_common.write_incident_state(state, make_current=False)
    print(f"{state['id']}: dismissed and closed.")
    print_close_out(state, doc, args.site_root)
    return 0


def cmd_gc(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    if args.ignore_issue_state and sre_common.from_chat():
        # The deny rule is text matching; this is the control. Closing the issue is a person's decision.
        raise SiteError("--ignore-issue-state clears state before a person has closed the issue; not allowed from chat")
    policy = state_gc.Policy(
        require_issue_closed=not args.ignore_issue_state, fetch=not args.no_fetch, token_days=args.token_days
    )
    result = state_gc.run(doc, args.site_root, policy, apply=args.apply, only=args.only)
    if args.quiet:
        return 0
    print(json.dumps(result, indent=2))
    if result["dry_run"] and result["cleared"]:
        print("Dry run: nothing was deleted. Run again with --apply to delete.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915  pylint: disable=too-many-statements
    # No abbreviated options: the chat policy matches option names as text (homelabia#273).
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--site", help="site repository path")
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="create the tracking issue")
    opener.add_argument("--title", required=True)
    opener.add_argument("--severity", choices=SEVERITIES, required=True, help="provisional severity")
    opener.add_argument("--affected", help="hosts or services affected, as far as known")
    opener.add_argument("--services", help="catalog services affected, comma-separated (name or component:<name>)")
    opener.add_argument("--impact", help="what is broken, for whom")
    opener.add_argument("--drill", action="store_true", help="practice incident, tracked in the drill repository")
    opener.add_argument("--new", action="store_true", help="open even though other incidents are open")
    opener.add_argument(
        "--auto", action="store_true", help="unattended triage: open in runner.tracker_repo (private), to be promoted"
    )
    opener.set_defaults(run=cmd_open)

    promote = sub.add_parser("promote", help="make an unattended triage a real incident on the site's tracker")
    promote.add_argument("--incident")
    promote.add_argument("--by", required=True, help="the responder who asked for it")
    promote.add_argument("--title", help="title for the public issue; default the triage's")
    promote.set_defaults(run=cmd_promote)

    dismiss = sub.add_parser("dismiss", help="close an unattended triage that is not an incident")
    dismiss.add_argument("--incident")
    dismiss.add_argument("--by", required=True, help="the responder who judged it")
    dismiss.add_argument("--reason", required=True)
    dismiss.set_defaults(run=cmd_dismiss)

    attach = sub.add_parser("attach", help="adopt an existing tracking issue")
    attach.add_argument("incident", help="<repo-alias>-<number>")
    attach.add_argument("--reopen", action="store_true", help="reopen it if it is resolved (recorded in the timeline)")
    attach.set_defaults(run=cmd_attach)

    status = sub.add_parser("status", help="show an incident's local state")
    status.add_argument("--incident")
    status.set_defaults(run=cmd_status)

    setter = sub.add_parser("set", help="edit facts in the issue body")
    setter.add_argument("--incident")
    setter.add_argument("--severity", choices=SEVERITIES)
    setter.add_argument("--final", action="store_true", help="mark the severity final rather than provisional")
    setter.add_argument("--phase", choices=PHASES)
    setter.add_argument("--affected")
    setter.add_argument("--services", help="catalog services affected, comma-separated; replaces the list")
    setter.add_argument("--impact")
    setter.add_argument("--runbook")
    setter.set_defaults(run=cmd_set)

    gc = sub.add_parser("gc", help="clear local state of incidents whose audit log has merged", allow_abbrev=False)
    gc.add_argument(
        "--incident",
        dest="only",
        action="append",
        metavar="ID",
        help="clear only this incident (repeatable); the default is every incident",
    )
    gc.add_argument("--apply", action="store_true", help="delete; the default is a dry run")
    gc.add_argument("--no-fetch", action="store_true", help="do not fetch the audit repository first")
    gc.add_argument("--ignore-issue-state", action="store_true", help="do not require the tracking issue to be closed")
    gc.add_argument("--token-days", type=int, default=7, help="age at which an unused bind token is removed")
    gc.add_argument("--quiet", action="store_true", help="print nothing")
    gc.set_defaults(run=cmd_gc)

    closer = sub.add_parser("close", help="mark the incident resolved")
    closer.add_argument("--incident")
    closer.set_defaults(run=cmd_close)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)

    def action() -> int:
        site_root, doc = load_site(args.site)
        args.site_root = site_root
        return args.run(args, doc)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
