#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Write the monthly reliability report: the product owner's SLA artefact.

One markdown file per month, with a section for every service whose tier
requires an SLA report. It is built only from data: the daily snapshots
`slo.py snapshot` writes, the catalog, and the incident tracker. The same
inputs always give the same report, and no model is involved.

For each service: its SLOs' attainment over the month (the mean of the daily
values), how many days were captured, the lowest budget remaining, the days
spent burning fast, exhausted and frozen, the month's incidents, and
recommendations from fixed rules. A service with no journey says so; it is
never shown as healthy.

An incident is an issue in the tracker whose body was written by
incident.py. It belongs to the services on its 'Services:' line, or, when
that line is missing, to the catalog names found in its 'Affected:' line.
An incident that matches nothing is listed as unattributed, never dropped.

Exit codes: 0 written, 1 written but snapshots are missing for more days
than --max-missing allows, 2 the site or the tracker could not be used.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import budget
import catalog_check
import incident
import slo
import sre_common
from sre_common import SiteError, guarded, load_site, load_yaml

WORD = re.compile(r"[a-z0-9][a-z0-9-]*")
SEVERITY = re.compile(r"^\*\*Severity:\*\* (P[1-4])", re.MULTILINE)
RESOLVED = re.compile(r"\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\)")
TIGHTEN_SHARE = 0.25
# A day with fewer measured minutes than this is reported as partly measured.
FULL_DAY_MINUTES = 1440 * 0.95


@dataclass
class Service:
    ref: str
    name: str
    tier: int
    purpose: str
    resources: set[str]


@dataclass
class Incident:  # pylint: disable=too-many-instance-attributes
    number: int
    title: str
    url: str
    severity: str
    opened: datetime
    resolved: str
    services: list[str]
    attributed_by: str


@dataclass
class Month:
    first: date
    last: date
    timezone: str

    @property
    def label(self) -> str:
        return self.first.strftime("%B %Y")

    def days(self, start: date | None = None, end: date | None = None) -> list[date]:
        lo, hi = max(self.first, start or self.first), min(self.last, end or self.last)
        return [lo + timedelta(days=n) for n in range((hi - lo).days + 1)] if lo <= hi else []


@dataclass
class Inputs:
    services: list[Service]
    slos: list[slo.Slo]
    history: list[dict[str, Any]]
    incidents: list[Incident]
    follow_ups: list[dict[str, Any]]
    first_seen: dict[str, date] = field(default_factory=dict)


def parse_month(text: str, timezone: str) -> Month:
    try:
        first = datetime.strptime(text, "%Y-%m").date()
    except ValueError as exc:
        raise SiteError(f"--month must be YYYY-MM, not {text!r}") from exc
    return Month(first, first.replace(day=calendar.monthrange(first.year, first.month)[1]), timezone)


# --- inputs -----------------------------------------------------------------------


def report_services(site_root: Path, doc: dict[str, Any]) -> tuple[list[Service], dict[str, set[str]]]:
    """Services whose tier requires an SLA report, and every Component's name and resources for attribution."""
    tiers_doc = load_yaml(site_root / doc["tiers_file"])
    needing = {tier["id"] for tier in tiers_doc.get("tiers", []) if "sla_report" in tier.get("requires", [])}
    report = catalog_check.CatalogReport()
    entities = catalog_check.load_entities(site_root / doc["catalog"]["path"], report)
    names: dict[str, set[str]] = {}
    services = []
    for entity in entities:
        if entity.doc["kind"] != "Component":
            continue
        resources = {ref.split(":", 1)[1] for ref in entity.spec.get("dependsOn", []) if ref.startswith("resource:")}
        names[entity.ref] = {entity.doc["metadata"]["name"], *resources}
        sre = entity.spec.get("sre") or {}
        if sre.get("tier") in needing:
            services.append(
                Service(entity.ref, entity.doc["metadata"]["name"], sre["tier"], sre.get("purpose", ""), resources)
            )
    return sorted(services, key=lambda s: (s.tier, s.name)), names


def attribute_from_affected(affected: str | None, names: dict[str, set[str]]) -> list[str]:
    """Services whose name, or one of whose resources' names, appears as a word in an 'Affected:' line."""
    words = set(WORD.findall((affected or "").lower()))
    return sorted(ref for ref, known in names.items() if words & known)


def to_incident(issue: dict[str, Any], names: dict[str, set[str]], timezone: str) -> Incident:
    body = issue.get("body") or ""
    services = incident.parse_services(body)
    attributed_by = "Services"
    if not services:
        services = attribute_from_affected(incident.read_field(body, "Affected"), names)
        attributed_by = "Affected" if services else "none"
    severity = SEVERITY.search(body)
    resolved_line = incident.read_field(body, "Resolved") or ""
    resolved_at = RESOLVED.search(resolved_line)
    if resolved_at:
        resolved = sre_common.parse_utc(resolved_at.group(1)).astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    elif issue.get("closedAt"):
        when = sre_common.parse_utc(issue["closedAt"]).astimezone(ZoneInfo(timezone))
        resolved = f"{when:%Y-%m-%d} (issue closed)"
    else:
        resolved = "not yet"
    return Incident(
        number=issue["number"],
        title=issue["title"],
        url=issue["url"],
        severity=severity.group(1) if severity else "?",
        opened=sre_common.parse_utc(issue["createdAt"]).astimezone(ZoneInfo(timezone)),
        resolved=resolved,
        services=services,
        attributed_by=attributed_by,
    )


def tracker_issues(doc: dict[str, Any]) -> list[dict[str, Any]]:
    repo = doc["repos"][doc["tracker"]["repo"]]["repo"]
    fields = "number,title,body,createdAt,closedAt,url,state"
    out = sre_common.run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            doc["tracker"]["incident_label"],
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            fields,
        ],
        timeout=120,
    )
    return json.loads(out or "[]")


def split_issues(
    issues: list[dict[str, Any]], names: dict[str, set[str]], month: Month
) -> tuple[list[Incident], list[dict[str, Any]]]:
    """This month's incidents, and the open issues carrying the incident label that are not incident records."""
    incidents, follow_ups = [], []
    for issue in issues:
        body = issue.get("body") or ""
        if incident.MARKER not in body:
            if issue.get("state") == "OPEN":
                follow_ups.append(issue)
            continue
        if issue["title"].startswith("[DRILL]"):
            continue
        one = to_incident(issue, names, month.timezone)
        if month.first <= one.opened.date() <= month.last:
            incidents.append(one)
    return sorted(incidents, key=lambda i: i.opened), sorted(follow_ups, key=lambda i: i["number"])


# --- per-service figures ------------------------------------------------------------


def pct(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value * 100:.{places}f}%"


def slo_figures(one: slo.Slo, inputs: Inputs, month: Month, yesterday: date) -> dict[str, Any]:
    """Attainment, coverage and budget days for one SLO over the month."""
    rows = sorted(
        (
            r
            for r in inputs.history
            if r["slo"] == one.name and month.first.isoformat() <= r["date"] <= month.last.isoformat()
        ),
        key=lambda r: r["date"],
    )
    first_seen = inputs.first_seen.get(one.name)
    expected = month.days(first_seen, yesterday) if first_seen else []
    captured = {r["date"] for r in rows}
    attainment = sum(r["attainment_1d"] for r in rows) / len(rows) if rows else None
    budgets = [(r["budget_remaining_min"], r["date"]) for r in rows if r.get("budget_remaining_min") is not None]
    return {
        "attainment": attainment,
        "expected": len(expected),
        "captured": len(captured),
        "missing": [d.isoformat() for d in expected if d.isoformat() not in captured],
        "lowest": min(budgets) if budgets else None,
        "burning": sum(r["state"] == "burning fast" for r in rows),
        "partial": sum(r.get("data_minutes", 0) < FULL_DAY_MINUTES for r in rows),
        "exhausted": sum(r["state"] == "exhausted" for r in rows),
        "since": first_seen,
    }


def frozen_days(service: str, inputs: Inputs, month: Month) -> int:
    """Days on which some SLO's exhausted budget froze this service."""
    days = {
        r["date"]
        for r in inputs.history
        if r.get("frozen")
        and service in r.get("freeze_scope", [])
        and month.first.isoformat() <= r["date"] <= month.last.isoformat()
    }
    return len(days)


def recommendations(figures: list[tuple[slo.Slo, dict[str, Any]]], incidents: list[Incident]) -> list[str]:
    """Fixed rules; the same figures always give the same advice."""
    out = []
    budgeted = [(one, fig) for one, fig in figures if one.budget]
    if not figures:
        if incidents:
            out.append(f"{len(incidents)} incident(s) this month and nothing measured: define a journey and SLO first.")
        else:
            out.append("No journey is defined, so nothing is measured. Define one with the define-journey skill.")
    for one, fig in budgeted:
        if fig["exhausted"]:
            out.append(
                f"{one.name}: budget exhausted on {fig['exhausted']} day(s). "
                "The error-budget policy puts reliability work on what spent it first."
            )
        elif fig["burning"]:
            out.append(f"{one.name}: burning fast on {fig['burning']} day(s). Find what spent the budget.")
        if fig["missing"]:
            out.append(
                f"{one.name}: snapshots missing for {len(fig['missing'])} day(s); attainment uses the days captured."
            )
        well_above = fig["attainment"] is not None and fig["attainment"] >= 1 - (1 - one.target) * TIGHTEN_SHARE
        if well_above and fig["captured"] >= 20 and not (fig["missing"] or fig["burning"] or fig["exhausted"]):
            out.append(f"{one.name}: well above target all month. Consider a tighter target at the next review.")
    return out or ["None."]


# --- rendering ------------------------------------------------------------------------


def incident_table(incidents: list[Incident]) -> list[str]:
    lines = ["| Incident | Severity | Opened | Resolved | Attributed from |", "|---|---|---|---|---|"]
    for one in incidents:
        lines.append(
            f"| [#{one.number}]({one.url}) {one.title} | {one.severity} | {one.opened:%Y-%m-%d %H:%M} "
            f"| {one.resolved} | {one.attributed_by} |"
        )
    return lines


def service_section(service: Service, inputs: Inputs, month: Month, yesterday: date) -> tuple[list[str], list[str]]:
    """The section for one service, and its row in the summary table."""
    own = [one for one in inputs.slos if f"component:{one.service}" == service.ref]
    figures = [(one, slo_figures(one, inputs, month, yesterday)) for one in own]
    incidents = [one for one in inputs.incidents if service.ref in one.services]
    lines = [f"### {service.name}", "", f"Tier {service.tier}. {service.purpose}".rstrip(), ""]
    if not figures:
        lines += ["No journey is defined for this service, so nothing is measured.", ""]
    for one, fig in figures:
        kind = "spends budget" if one.budget else "informational, no budget"
        lines += [f"**SLO `{one.name}`**: {pct(one.target, 1)} over {one.window}, {kind}.", ""]
        if fig["since"] is None:
            lines += ["- No snapshot has been taken yet.", ""]
            continue
        lines.append(
            f"- Attainment: **{pct(fig['attainment'])}**, the mean of {fig['captured']} daily value(s); "
            f"{fig['captured']} of {fig['expected']} day(s) captured since data began on {fig['since'].isoformat()}."
        )
        if fig["partial"]:
            lines.append(f"- {fig['partial']} of those day(s) were only partly measured (under 95% of the day).")
        if one.budget:
            lowest = f"{pct(fig['lowest'][0], 0)} on {fig['lowest'][1]}" if fig["lowest"] else "n/a"
            lines += [
                f"- Lowest budget remaining: {lowest}.",
                f"- Days burning fast: {fig['burning']}. Days exhausted: {fig['exhausted']}.",
            ]
        lines.append("")
    frozen = frozen_days(service.ref, inputs, month)
    lines += [f"Days frozen, with Tier 2 off: {frozen}.", "", "**Incidents**", ""]
    lines += [*incident_table(incidents), ""] if incidents else ["None this month.", ""]
    lines += ["**Recommendations**", "", *(f"- {item}" for item in recommendations(figures, incidents)), ""]

    budgeted = [(one, fig) for one, fig in figures if one.budget]
    if budgeted:
        one, fig = budgeted[0]
        row = [
            service.name,
            f"`{one.name}`" + (f" (+{len(budgeted) - 1})" if len(budgeted) > 1 else ""),
            pct(one.target, 1),
            pct(fig["attainment"]),
            pct(fig["lowest"][0], 0) if fig["lowest"] else "n/a",
            str(fig["exhausted"]),
        ]
    else:
        row = [service.name, "none defined", "", "", "", ""]
    return lines, [*row, str(frozen), str(len(incidents))]


def render(site_name: str, month: Month, inputs: Inputs, yesterday: date) -> str:
    sections, rows = [], []
    for service in inputs.services:
        section, row = service_section(service, inputs, month, yesterday)
        sections += section
        rows.append("| " + " | ".join(row) + " |")
    reported = {service.ref for service in inputs.services}
    unattributed = [one for one in inputs.incidents if not one.services]
    elsewhere = [one for one in inputs.incidents if one.services and not set(one.services) & reported]
    out = [
        f"# Reliability report: {month.label}",
        "",
        f"Site **{site_name}**, {month.first.isoformat()} to {month.last.isoformat()} ({month.timezone}). "
        "Generated by the sre-harness `report.py` from daily budget snapshots, the catalog and the incident "
        "tracker; do not edit it by hand. Merging it is the owner's acceptance of the month's report.",
        "",
        "## Summary",
        "",
        "| Service | SLO | Target | Attainment | Lowest budget | Days exhausted | Days frozen | Incidents |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        f"{len(inputs.incidents)} incident(s) opened this month; {len(unattributed)} unattributed.",
        "",
        "## Services",
        "",
        *sections,
        "## Unattributed incidents",
        "",
    ]
    if unattributed:
        out += [
            *incident_table(unattributed),
            "",
            "- Attribute them with `incident.py set --services`, or catalogue what they affected.",
            "",
        ]
    else:
        out += ["None.", ""]
    out += ["## Incidents on other services", ""]
    out += [*incident_table(elsewhere), ""] if elsewhere else ["None.", ""]
    out += ["## Other open issues with the incident label", ""]
    if inputs.follow_ups:
        out += [f"- [#{issue['number']}]({issue['url']}) {issue['title']}" for issue in inputs.follow_ups] + [""]
    else:
        out += ["None.", ""]
    return "\n".join(out).rstrip() + "\n"


# --- command ---------------------------------------------------------------------------


def build(site_root: Path, doc: dict[str, Any], month: Month, history: Path, yesterday: date) -> tuple[str, int]:
    """The report text and the largest number of days missing for any budgeted SLO."""
    found = slo.load(site_root, doc)
    if found.errors:
        raise SiteError(f"journeys or SLOs are not valid ({len(found.errors)} problem(s)); run slo.py check")
    services, names = report_services(site_root, doc)
    rows = budget.read_history(history)
    first_seen: dict[str, date] = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        first_seen[row["slo"]] = min(first_seen.get(row["slo"], day), day)
    incidents, follow_ups = split_issues(tracker_issues(doc), names, month)
    inputs = Inputs(services, found.slos, rows, incidents, follow_ups, first_seen)
    text = render(doc["site"]["name"], month, inputs, yesterday)
    missing = max(
        (len(slo_figures(one, inputs, month, yesterday)["missing"]) for one in found.slos if one.budget), default=0
    )
    return text, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--month", help="YYYY-MM; default is last month in the site's time zone")
    parser.add_argument("--history", required=True, help="the snapshot history directory")
    parser.add_argument("--out", help="file to write; default is standard output")
    parser.add_argument("--max-missing", type=int, default=3, help="days of snapshots a budgeted SLO may lack")
    args = parser.parse_args(argv)

    def action() -> int:
        site_root, doc = load_site(args.site)
        timezone = doc["incident"]["timezone"]
        today = datetime.now(ZoneInfo(timezone)).date()
        month = parse_month(args.month or f"{today.replace(day=1) - timedelta(days=1):%Y-%m}", timezone)
        history = Path(args.history)
        if not history.is_dir():
            raise SiteError(f"history directory {history} does not exist")
        text, missing = build(site_root, doc, month, history, today - timedelta(days=1))
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            sys.stdout.write(text)
        if missing > args.max_missing:
            print(f"ERROR snapshots are missing for {missing} day(s), more than {args.max_missing}", file=sys.stderr)
            return 1
        return 0

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
