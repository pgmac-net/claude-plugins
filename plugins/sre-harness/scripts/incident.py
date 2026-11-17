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
  gc       clear local state of incidents whose audit log has merged (dry run unless --apply)

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

import sre_common
import state_gc
from sre_common import SEVERITIES, SiteError, guarded, iso_utc, load_site, local_clock, split_incident_id, utc_now

MARKER = "<!-- sre-harness:incident v1 -->"
PHASES = ("Establishing picture", "Diagnosing", "Mitigating", "Verifying", "Resolved")
LABEL_COLOUR = "b60205"
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


def tracker(doc: dict[str, Any], drill: bool) -> tuple[str, str, str]:
    """Return (repo alias, code-host repository, label) for real or practice incidents."""
    if drill:
        settings = doc["incident"].get("drill")
        if not settings:
            raise SiteError("site.yaml has no incident.drill section; cannot open a drill")
        alias, label = settings["repo"], settings["label"]
    else:
        alias, label = doc["tracker"]["repo"], doc["tracker"]["incident_label"]
    return alias, doc["repos"][alias]["repo"], label


def open_incidents(repo: str, label: str) -> list[dict[str, Any]]:
    """Open tracking issues carrying the incident label."""
    out = sre_common.run_gh(
        ["issue", "list", "--repo", repo, "--label", label, "--state", "open", "--json", "number,title,url"]
    )
    return json.loads(out or "[]")


def ensure_label(repo: str, label: str, drill: bool) -> None:
    """Create the tracking label if the repository lacks it."""
    description = "Practice incident, not a real one" if drill else "Work arising from a production incident"
    try:
        sre_common.run_gh(
            ["label", "create", label, "--repo", repo, "--color", LABEL_COLOUR, "--description", description]
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


def cmd_open(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    cleared = state_gc.auto_clean(doc, args.site_root)
    if cleared:
        print(f"Cleared local state of {cleared} earlier incident(s) whose audit log has merged.", file=sys.stderr)
    alias, repo, label = tracker(doc, args.drill)
    existing = open_incidents(repo, label)
    if existing and not args.new:
        print(f"{len(existing)} open incident(s) in {repo}. Attach to one, or pass --new if this is separate:")
        for issue in existing:
            print(f"  {alias}-{issue['number']}  {issue['title']}  {issue['url']}")
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
    ensure_label(repo, label, args.drill)
    title = f"[DRILL] {args.title}" if args.drill else args.title
    url = sre_common.run_gh(
        ["issue", "create", "--repo", repo, "--title", title, "--label", label, "--body-file", "-"], stdin=body
    ).strip()
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    state = {
        "id": f"{alias}-{number}",
        "repo": repo,
        "number": number,
        "url": url,
        "title": title,
        "drill": args.drill,
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
    state = {
        "id": args.incident,
        "repo": repo,
        "number": number,
        "url": issue["url"],
        "title": issue["title"],
        "drill": any(label["name"] == drill.get("label") for label in issue.get("labels", [])),
        "severity": severity.group(1) if severity else None,
        "phase": phase.group(1).strip() if phase else None,
        "opened_at": iso_utc(sre_common.parse_utc(issue["createdAt"])),
        "status": "open",
    }
    start_capture(state, doc)
    token = state.pop("bind")
    sre_common.write_incident_state(state)
    print(json.dumps({"id": state["id"], "url": state["url"], "bind": token}))
    return 0


def cmd_status(args: argparse.Namespace, _doc: dict[str, Any]) -> int:
    print(json.dumps(sre_common.read_incident_state(args.incident), indent=2))
    return 0


def apply_changes(state: dict[str, Any], changes: dict[str, str], final: bool = False) -> None:
    """Edit the issue body in place and mirror severity and phase into local state."""
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
    print("The audit hook is still capturing. When the review is written, run: audit.py commit")
    return 0


def cmd_gc(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    policy = state_gc.Policy(
        require_issue_closed=not args.ignore_issue_state, fetch=not args.no_fetch, token_days=args.token_days
    )
    result = state_gc.run(doc, args.site_root, policy, apply=args.apply)
    if args.quiet:
        return 0
    print(json.dumps(result, indent=2))
    if result["dry_run"] and result["cleared"]:
        print("Dry run: nothing was deleted. Run again with --apply to delete.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    opener.set_defaults(run=cmd_open)

    attach = sub.add_parser("attach", help="adopt an existing tracking issue")
    attach.add_argument("incident", help="<repo-alias>-<number>")
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

    gc = sub.add_parser("gc", help="clear local state of incidents whose audit log has merged")
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
