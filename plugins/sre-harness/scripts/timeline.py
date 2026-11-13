#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Write and read the timeline of an incident.

The timeline is the comments on the incident's tracking issue. Each entry is
one comment: a hidden marker line that carries the entry type and UTC time,
then a line people read. This script is the only writer, so the format is
never rolled by hand, and any agent can use it.

Commands:
  add       post one typed entry
  handover  post a handover entry from its fixed fields
  list      print the entries as JSON
  due       say whether a status update is due for the incident's severity
  export    render the timeline as markdown, for a review document

A comment without a marker (typed by a person on the issue) is kept and
shown as a 'note'. So is a comment that carries a marker but was not posted
by a trusted author: on a public tracker anyone can comment, and a stranger
must not be able to forge a typed entry or its time.

Exit codes: 0 ok, 2 the site or the code host could not be used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import sre_common
from sre_common import SiteError, guarded, iso_utc, load_site, local_clock, parse_utc, utc_now

TYPES = ("discovery", "milestone", "action", "update", "handover", "summary", "decision")
STATUS_TYPES = ("update", "handover", "summary")
MARKER = re.compile(r"^<!-- sre-harness:timeline v1 type=([a-z]+) at=(\S+) -->\s*$")
VISIBLE = re.compile(r"^\*\*[^*]+\*\* — ?")
HANDOVER_FIELDS = (
    ("state", "State"),
    ("tried", "Tried"),
    ("hypotheses", "Open hypotheses"),
    ("next", "Next step"),
    ("holder", "Now with"),
)


def render(entry_type: str, text: str, moment, timezone: str) -> str:
    """The comment body for one entry."""
    marker = f"<!-- sre-harness:timeline v1 type={entry_type} at={iso_utc(moment)} -->"
    return f"{marker}\n**{local_clock(moment, timezone)} · {entry_type.capitalize()}** — {text.strip()}\n"


TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def parse(body: str, created_at: str, author: str, trusted: bool = True) -> dict[str, Any]:
    """Turn one issue comment into a timeline entry.

    A marker is honoured only from a trusted author. On a public tracker anyone
    can comment, so a marker from anyone else is kept as a note and flagged. An
    entry may be backdated, but its time is never later than when the comment
    was posted.
    """
    posted = parse_utc(created_at)
    lines = body.replace("\r\n", "\n").split("\n")
    match = MARKER.match(lines[0]) if lines else None
    note = {"type": "note", "at": iso_utc(posted), "text": body.strip(), "author": author}
    if not match or match.group(1) not in TYPES:
        return note
    if not trusted:
        return {**note, "text": "\n".join(lines[1:]).strip(), "untrusted_marker": True}
    try:
        claimed = parse_utc(match.group(2))
    except ValueError:
        return note
    text = VISIBLE.sub("", "\n".join(lines[1:]).strip(), count=1)
    return {"type": match.group(1), "at": iso_utc(min(claimed, posted)), "text": text, "author": author}


def fetch(state: dict[str, Any], responders: list[str] | None = None) -> list[dict[str, Any]]:
    """Every entry of an incident's timeline, oldest first.

    Trusted authors are the repository's owner, members and collaborators, plus
    any login the site lists under incident.responders.
    """
    out = sre_common.run_gh(
        [
            "api",
            f"repos/{state['repo']}/issues/{state['number']}/comments",
            "--paginate",
            "--jq",
            ".[] | {body: .body, created_at: .created_at, author: .user.login, association: .author_association}",
        ]
    )
    allowed = {login.lower() for login in responders or []}
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        comment = json.loads(line)
        trusted = comment.get("association") in TRUSTED_ASSOCIATIONS or comment["author"].lower() in allowed
        entries.append(parse(comment["body"], comment["created_at"], comment["author"], trusted))
    return sorted(entries, key=lambda entry: entry["at"])


def post(state: dict[str, Any], entry_type: str, text: str, timezone: str, at: str | None) -> str:
    """Post one entry and return the comment URL."""
    if not text.strip():
        raise SiteError("an entry needs text")
    moment = parse_utc(at) if at else utc_now()
    body = render(entry_type, text, moment, timezone)
    return sre_common.run_gh(
        ["issue", "comment", str(state["number"]), "--repo", state["repo"], "--body-file", "-"], stdin=body
    ).strip()


def update_due(state: dict[str, Any], entries: list[dict[str, Any]], cadence: dict[str, int], now) -> dict[str, Any]:
    """Whether a status update is owed, given the site's cadence for this severity."""
    severity = state.get("severity")
    limit = cadence.get(severity or "")
    if state.get("status") == "resolved" or limit is None:
        return {"due": False, "reason": f"no scheduled updates for {severity or 'an unset severity'}"}
    stamps = [entry["at"] for entry in entries if entry["type"] in STATUS_TYPES and parse_utc(entry["at"]) <= now]
    last = parse_utc(max(stamps)) if stamps else parse_utc(state["opened_at"])
    waited = int((now - last).total_seconds() // 60)
    return {
        "due": waited >= limit,
        "severity": severity,
        "every_minutes": limit,
        "minutes_since_last": waited,
        "minutes_until_due": max(limit - waited, 0),
    }


def export(state: dict[str, Any], entries: list[dict[str, Any]], timezone: str) -> str:
    """Markdown rendering of the timeline, grouped by local date."""
    zone = ZoneInfo(timezone)
    lines = [f"## Timeline: {state['title']}", "", f"Source: {state['url']}. Times are {timezone}.", ""]
    day = None
    for entry in entries:
        local = parse_utc(entry["at"]).astimezone(zone)
        if local.date() != day:
            day = local.date()
            lines += [f"### {day.isoformat()}", ""]
        head, *rest = entry["text"].split("\n")
        label = entry["type"].capitalize()
        if entry["type"] == "note":
            label = f"Note by {entry['author']}" + (
                " (marker ignored: not a trusted author)" if entry.get("untrusted_marker") else ""
            )
        lines.append(f"- **{local.strftime('%H:%M')}** · {label} — {head}")
        lines += [f"  {line}" for line in rest]
    counts = {name: sum(1 for entry in entries if entry["type"] == name) for name in (*TYPES, "note")}
    summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    lines += ["", f"{len(entries)} entries: {summary or 'none'}."]
    return "\n".join(lines) + "\n"


def cmd_add(args, state, doc) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    print(post(state, args.type, text, doc["incident"]["timezone"], args.at))
    return 0


def cmd_handover(args, state, doc) -> int:
    text = "Passing the incident on.\n" + "\n".join(
        f"- **{label}:** {getattr(args, key)}" for key, label in HANDOVER_FIELDS
    )
    print(post(state, "handover", text, doc["incident"]["timezone"], args.at))
    return 0


def cmd_list(_args, state, doc) -> int:
    print(json.dumps(fetch(state, doc["incident"].get("responders")), indent=2))
    return 0


def cmd_due(_args, state, doc) -> int:
    entries = fetch(state, doc["incident"].get("responders"))
    result = update_due(state, entries, doc["incident"].get("update_minutes", {}), utc_now())
    print(json.dumps(result))
    return 0


def cmd_export(args, state, doc) -> int:
    text = export(state, fetch(state, doc["incident"].get("responders")), doc["incident"]["timezone"])
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--incident", help="<repo-alias>-<number>; default is the current incident")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="post one typed entry")
    add.add_argument("--type", choices=TYPES, required=True)
    add.add_argument("--text", help="entry text; read from stdin when omitted")
    add.add_argument("--at", help="when it happened, ISO 8601; default now")
    add.set_defaults(run=cmd_add)

    handover = sub.add_parser("handover", help="post a handover entry")
    for key, label in HANDOVER_FIELDS:
        handover.add_argument(f"--{key}", required=True, help=label)
    handover.add_argument("--at", help="ISO 8601; default now")
    handover.set_defaults(run=cmd_handover)

    sub.add_parser("list", help="print entries as JSON").set_defaults(run=cmd_list)
    sub.add_parser("due", help="is a status update due?").set_defaults(run=cmd_due)

    exporter = sub.add_parser("export", help="render the timeline as markdown")
    exporter.add_argument("--out", help="write to this file instead of stdout")
    exporter.set_defaults(run=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)

    def action() -> int:
        _site_root, doc = load_site(args.site)
        return args.run(args, sre_common.read_incident_state(args.incident), doc)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
