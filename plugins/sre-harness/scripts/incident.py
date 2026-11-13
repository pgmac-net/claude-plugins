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
  set      change severity, phase, affected scope, impact or runbook in the issue body
  close    mark the incident resolved locally and in the body; the issue stays open

Exit codes: 0 ok, 2 the site or the code host could not be used, 3 open
incidents already exist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import sre_common
from sre_common import SEVERITIES, SiteError, guarded, iso_utc, load_site, local_clock, split_incident_id, utc_now

MARKER = "<!-- sre-harness:incident v1 -->"
PHASES = ("Establishing picture", "Diagnosing", "Mitigating", "Verifying", "Resolved")
LABEL_COLOUR = "b60205"
FIELDS = {"severity": "Severity", "phase": "Phase", "affected": "Affected"}
SECTIONS = {"impact": "Impact", "runbook": "Runbook"}

BODY = """{marker}
## Incident

**Detected:** {clock} ({utc})
**Severity:** {severity} (provisional)
**Phase:** {phase}
**Affected:** {affected}

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


def set_field(body: str, name: str, value: str) -> str:
    """Replace one '**Name:** value' line in the issue body."""
    pattern = re.compile(rf"^\*\*{re.escape(name)}:\*\* .*$", re.MULTILINE)
    if not pattern.search(body):
        raise SiteError(f"issue body has no '**{name}:**' line; it was not opened by incident.py")
    return pattern.sub(lambda _match: f"**{name}:** {value}", body, count=1)


def set_section(body: str, heading: str, text: str) -> str:
    """Replace the text under one '## Heading' in the issue body."""
    pattern = re.compile(rf"(^## {re.escape(heading)}\n\n)(.*?)(?=\n## |\n---\n|\Z)", re.MULTILINE | re.DOTALL)
    if not pattern.search(body):
        raise SiteError(f"issue body has no '## {heading}' section; it was not opened by incident.py")
    return pattern.sub(lambda match: f"{match.group(1)}{text.strip()}\n", body, count=1)


def cmd_open(args: argparse.Namespace, doc: dict[str, Any]) -> int:
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
        affected=args.affected or "not yet known",
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
    sre_common.write_incident_state(state)
    print(json.dumps({"id": state["id"], "url": url}))
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
    sre_common.write_incident_state(state)
    print(json.dumps({"id": state["id"], "url": state["url"]}))
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
        else:
            body = set_section(body, SECTIONS[key], value)
    sre_common.run_gh(["issue", "edit", str(state["number"]), "--repo", state["repo"], "--body-file", "-"], stdin=body)
    state.update({key: changes[key] for key in ("severity", "phase") if key in changes})


def cmd_set(args: argparse.Namespace, _doc: dict[str, Any]) -> int:
    state = sre_common.read_incident_state(args.incident)
    changes = {key: getattr(args, key) for key in (*FIELDS, *SECTIONS) if getattr(args, key) is not None}
    if not changes:
        raise SiteError("nothing to change; pass at least one of --severity, --phase, --affected, --impact, --runbook")
    apply_changes(state, changes, args.final)
    sre_common.write_incident_state(state, make_current=False)
    print(f"{state['id']}: updated {', '.join(changes)}")
    return 0


def cmd_close(args: argparse.Namespace, _doc: dict[str, Any]) -> int:
    state = sre_common.read_incident_state(args.incident)
    apply_changes(state, {"phase": "Resolved"})
    state.update({"status": "resolved", "resolved_at": iso_utc(utc_now())})
    sre_common.write_incident_state(state, make_current=False)
    print(f"{state['id']}: resolved. The tracking issue stays open until a person closes it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="create the tracking issue")
    opener.add_argument("--title", required=True)
    opener.add_argument("--severity", choices=SEVERITIES, required=True, help="provisional severity")
    opener.add_argument("--affected", help="hosts or services affected, as far as known")
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
    setter.add_argument("--impact")
    setter.add_argument("--runbook")
    setter.set_defaults(run=cmd_set)

    closer = sub.add_parser("close", help="mark the incident resolved")
    closer.add_argument("--incident")
    closer.set_defaults(run=cmd_close)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)

    def action() -> int:
        _site_root, doc = load_site(args.site)
        return args.run(args, doc)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
