#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Rank the recommendations backlog from PIR trends and open action items.

Rule-based, no model: the same PIRs, issues and catalog always give the same
backlog. The backlog itself is the set of issues carrying an action label
(by default 'incident' and 'design-review'); this ranks them and points at gaps.

  - Recurring themes: a service or PIR tag on at least theme_min_pirs PIRs
    inside the rolling window.
  - Stale action items: open for longer than stale_action_days, highest
    priority and oldest first.
  - Repeat incidents: a service with two or more PIRs in the window that
    still has open action items.
  - Design reviews due: a Tier 1 service that is a recurring theme with no
    STPA analysis, or whose analysis predates three of its PIRs, has proposed
    changes waiting, or has uncontrolled scenarios with no issue.

A PIR belongs to the services on its 'services:' front-matter line (catalog
names). With none, its tags are matched against catalog names and resources.
A PIR that matches nothing is listed as unattributed, never dropped. An
action item belongs to the services of the PIR or analysis its body links.

What could not be read (the PIR checkout, a private repository the token
cannot see, the issue search) is said in the output, never silently skipped.

Exit codes: 0 written, 2 the site could not be used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import catalog_check
import sre_common
import stpa
from sre_common import SiteError, guarded, load_site

PRIORITIES = ("urgent", "high", "medium", "low")
PIR_LINK = re.compile(r"/([0-9]{4}-[0-9]{2}-[0-9]{2}-[A-Za-z0-9._-]+?)\.md")
ANALYSIS_LINK = re.compile(r"analyses/stpa/([a-z0-9][a-z0-9-]*)\.md")
MAX_TAG_THEMES = 10
REVIEW_PIRS = 3


@dataclass
class Pir:  # pylint: disable=too-many-instance-attributes
    stem: str
    title: str
    day: date
    severity: str
    tags: list[str]
    services: list[str]
    attributed_by: str
    url: str


@dataclass
class ActionItem:  # pylint: disable=too-many-instance-attributes
    repo: str
    number: int
    title: str
    url: str
    open: bool
    created: date
    closed: date | None
    priority: str
    services: list[str]


@dataclass
class Theme:
    name: str
    count: int
    latest: date
    pirs: list[str]


@dataclass
class Backlog:  # pylint: disable=too-many-instance-attributes
    as_of: date
    window_start: date
    settings: dict[str, Any]
    pirs_in_window: int = 0
    items_read: int = 0
    service_themes: list[Theme] = field(default_factory=list)
    tag_themes: list[Theme] = field(default_factory=list)
    stale: list[ActionItem] = field(default_factory=list)
    repeats: list[tuple[str, int, list[ActionItem]]] = field(default_factory=list)
    reviews_due: list[tuple[str, list[str]]] = field(default_factory=list)
    unattributed: list[Pir] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- inputs -------------------------------------------------------------------------------


def component_names(site_root: Path, doc: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Every Component's name and resource names, for attribution, and each one's tier."""
    report = catalog_check.CatalogReport()
    entities = catalog_check.load_entities(site_root / doc["catalog"]["path"], report)
    names: dict[str, set[str]] = {}
    tiers: dict[str, int] = {}
    for entity in entities:
        if entity.doc["kind"] != "Component":
            continue
        resources = {ref.split(":", 1)[1] for ref in entity.spec.get("dependsOn", []) if ref.startswith("resource:")}
        names[entity.ref] = {entity.doc["metadata"]["name"], *resources}
        tier = (entity.spec.get("sre") or {}).get("tier")
        if isinstance(tier, int):
            tiers[entity.ref] = tier
    return names, tiers


def pir_location(doc: dict[str, Any], override: str | None) -> tuple[Path | None, str]:
    """The directory holding PIRs, and the web base for links to them."""
    repo = doc["repos"].get(doc["tracker"]["repo"], {})
    sub = repo.get("pirs_path", "")
    web = f"https://github.com/{repo['repo']}/blob/HEAD/{sub}".rstrip("/") if repo.get("repo") else ""
    if override:
        return Path(override).expanduser(), web
    if not repo.get("path") or not sub:
        return None, web
    return Path(repo["path"]).expanduser() / sub, web


def attribute(meta: dict[str, Any], names: dict[str, set[str]], notes: list[str], stem: str) -> tuple[list[str], str]:
    by_name = {ref.split(":", 1)[1]: ref for ref in names}
    listed = meta.get("services") or []
    if listed:
        refs = []
        for name in listed:
            ref = by_name.get(str(name).removeprefix("component:"))
            if ref:
                refs.append(ref)
            else:
                notes.append(f"PIR {stem} names '{name}', which is not a catalog service")
        if refs:
            return sorted(set(refs)), "services"
    tags = {str(tag).lower() for tag in meta.get("tags") or []}
    found = sorted(ref for ref, known in names.items() if tags & known)
    return found, ("tags" if found else "none")


def load_pirs(folder: Path | None, web: str, names: dict[str, set[str]], notes: list[str]) -> list[Pir]:
    if folder is None:
        notes.append("PIRs not read: the tracker repository has no local path and no --pirs was given")
        return []
    if not folder.is_dir():
        notes.append(f"PIRs not read: {folder} is not a directory")
        return []
    out = []
    for path in sorted(folder.glob("*.md")):
        meta, _ = stpa.split_front_matter(path.read_text(encoding="utf-8"))
        if not meta or "date" not in meta:
            continue
        try:
            day = date.fromisoformat(str(meta["date"]))
        except ValueError:
            notes.append(f"PIR {path.stem} has a date that is not YYYY-MM-DD; skipped")
            continue
        services, how = attribute(meta, names, notes, path.stem)
        out.append(
            Pir(
                stem=path.stem,
                title=str(meta.get("title") or path.stem),
                day=day,
                severity=str(meta.get("severity") or "?"),
                tags=sorted({str(tag).lower() for tag in meta.get("tags") or []}),
                services=services,
                attributed_by=how,
                url=f"{web}/{path.name}" if web else path.name,
            )
        )
    return out


def unreadable_repos(doc: dict[str, Any]) -> list[str]:
    """Private repositories in the site the current token cannot see. Their action items are missing."""
    missing = []
    for alias, repo in sorted(doc["repos"].items()):
        if repo.get("visibility") != "private":
            continue
        try:
            sre_common.run_gh(["api", f"repos/{repo['repo']}", "--jq", ".full_name"], timeout=30)
        except SiteError:
            missing.append(f"{alias} ({repo['repo']})")
    return missing


def search_items(doc: dict[str, Any], labels: list[str]) -> list[dict[str, Any]]:
    owners = sorted({repo["repo"].split("/", 1)[0] for repo in doc["repos"].values()})
    fields = "number,title,url,state,createdAt,closedAt,labels,repository,body"
    seen: dict[str, dict[str, Any]] = {}
    for owner in owners:
        for label in labels:
            out = sre_common.run_gh(
                ["search", "issues", "--owner", owner, "--label", label, "--limit", "1000", "--json", fields],
                timeout=120,
            )
            for issue in json.loads(out or "[]"):
                seen.setdefault(issue["url"], issue)
    return [seen[url] for url in sorted(seen)]


def to_item(issue: dict[str, Any], pirs: dict[str, Pir], timezone: str) -> ActionItem:
    body = issue.get("body") or ""
    services = {ref for stem in PIR_LINK.findall(body) if stem in pirs for ref in pirs[stem].services}
    services |= {f"component:{name}" for name in ANALYSIS_LINK.findall(body)}
    labels = {(label.get("name") or "").lower() for label in issue.get("labels") or []}
    priority = next((p for p in PRIORITIES if f"priority:{p}" in labels or p in labels), "none")
    zone = ZoneInfo(timezone)
    closed = issue.get("closedAt")
    return ActionItem(
        repo=(issue.get("repository") or {}).get("nameWithOwner", ""),
        number=issue["number"],
        title=issue["title"],
        url=issue["url"],
        open=str(issue.get("state", "")).lower() == "open",
        created=sre_common.parse_utc(issue["createdAt"]).astimezone(zone).date(),
        closed=sre_common.parse_utc(closed).astimezone(zone).date() if closed else None,
        priority=priority,
        services=sorted(services),
    )


# --- ranking ------------------------------------------------------------------------------


def themes(pirs: list[Pir], keys: Any, minimum: int) -> list[Theme]:
    groups: dict[str, list[Pir]] = {}
    for pir in pirs:
        for key in keys(pir):
            groups.setdefault(key, []).append(pir)
    found = [
        Theme(key, len(group), max(p.day for p in group), [p.stem for p in sorted(group, key=lambda p: p.day)])
        for key, group in groups.items()
        if len(group) >= minimum
    ]
    return sorted(found, key=lambda t: (-t.count, -t.latest.toordinal(), t.name))


def reviews_due(
    tiers: dict[str, int], service_themes: list[Theme], pirs: list[Pir], analyses: list[stpa.Analysis]
) -> list[tuple[str, list[str]]]:
    by_service = {a.service: a for a in analyses if a.service}
    themed = {t.name: t.count for t in service_themes}
    out = []
    for ref in sorted(ref for ref, tier in tiers.items() if tier == 1):
        reasons = []
        analysis = by_service.get(ref)
        if analysis is None:
            if ref in themed:
                reasons.append(f"recurring theme ({themed[ref]} PIRs) and no STPA analysis")
        else:
            if analysis.errors:
                reasons.append(f"analysis has {len(analysis.errors)} error(s); run stpa.py check")
            reviewed = analysis.reviewed
            since = [p for p in pirs if ref in p.services and reviewed and p.day > reviewed]
            if analysis.meta.get("status") != "reviewed":
                reasons.append("analysis is still a draft")
            elif len(since) >= REVIEW_PIRS:
                reasons.append(f"{len(since)} PIRs since the last review on {reviewed.isoformat()}")
            if analysis.meta.get("proposed"):
                reasons.append(f"{len(analysis.meta['proposed'])} change(s) proposed by PIRs wait for review")
            if not analysis.errors:
                unfiled = [f for f in stpa.findings(analysis.meta) if not f["finding"]]
                if unfiled:
                    reasons.append(f"{len(unfiled)} uncontrolled scenario(s) with no issue")
        if reasons:
            out.append((ref, reasons))
    return out


def rank(  # noqa: PLR0913  pylint: disable=too-many-arguments
    as_of: date,
    conf: dict[str, Any],
    pirs: list[Pir],
    items: list[ActionItem],
    *,
    tiers: dict[str, int],
    analyses: list[stpa.Analysis],
) -> Backlog:
    start = as_of - timedelta(days=conf["theme_window_days"])
    backlog = Backlog(as_of=as_of, window_start=start, settings=conf)
    window = [p for p in pirs if start < p.day <= as_of]
    backlog.pirs_in_window = len(window)
    backlog.items_read = len(items)
    minimum = conf["theme_min_pirs"]
    backlog.service_themes = themes(window, lambda p: p.services, minimum)
    service_names = {ref.split(":", 1)[1] for ref in tiers}
    ignored = set(conf["ignore_tags"]) | service_names
    backlog.tag_themes = themes(window, lambda p: [t for t in p.tags if t not in ignored], minimum)[:MAX_TAG_THEMES]
    stale_before = as_of - timedelta(days=conf["stale_action_days"])
    backlog.stale = sorted(
        (i for i in items if i.open and i.created < stale_before),
        key=lambda i: ((PRIORITIES + ("none",)).index(i.priority), i.created, i.url),
    )
    counts: dict[str, int] = {}
    for pir in window:
        for ref in pir.services:
            counts[ref] = counts.get(ref, 0) + 1
    for ref, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        still_open = [i for i in items if i.open and ref in i.services]
        if count >= 2 and still_open:
            backlog.repeats.append((ref, count, still_open))
    backlog.reviews_due = reviews_due(tiers, backlog.service_themes, pirs, analyses)
    backlog.unattributed = [p for p in window if not p.services]
    return backlog


def build(site_root: Path, doc: dict[str, Any], as_of: date, pirs_dir: str | None = None) -> Backlog:
    conf = stpa.settings(doc)
    notes: list[str] = []
    names, tiers = component_names(site_root, doc)
    folder, web = pir_location(doc, pirs_dir)
    pirs = load_pirs(folder, web, names, notes)
    by_stem = {p.stem: p for p in pirs}
    items: list[ActionItem] = []
    try:
        unread = unreadable_repos(doc)
        if unread:
            notes.append("action items not read from private repositories the token cannot see: " + ", ".join(unread))
        raw = search_items(doc, conf["action_labels"])
        items = [to_item(issue, by_stem, doc["incident"]["timezone"]) for issue in raw]
    except SiteError as exc:
        notes.append(f"action items not read: {exc}")
    analyses = stpa.load_analyses(site_root, doc, set(names))
    backlog = rank(as_of, conf, pirs, items, tiers=tiers, analyses=analyses)
    backlog.notes = notes
    return backlog


# --- rendering ------------------------------------------------------------------------------


def _short(ref: str) -> str:
    return ref.split(":", 1)[-1]


def _item(item: ActionItem) -> str:
    return f"[{item.repo}#{item.number}]({item.url}) {item.title}"


def _themes_part(backlog: Backlog, h: str) -> list[str]:
    lines = [f"{h} Recurring themes", ""]
    if not (backlog.service_themes or backlog.tag_themes):
        minimum = backlog.settings["theme_min_pirs"]
        return [*lines, f"None: no service or tag on {minimum} or more PIRs in the window.", ""]
    lines += ["| Theme | Count | Latest | PIRs |", "|---|---|---|---|"]
    for kind, found in (("service", backlog.service_themes), ("tag", backlog.tag_themes)):
        lines += [
            f"| {kind} `{_short(t.name)}` | {t.count} | {t.latest.isoformat()} | {', '.join(t.pirs)} |" for t in found
        ]
    return [*lines, ""]


def _stale_part(backlog: Backlog, h: str) -> list[str]:
    lines = [f"{h} Stale action items", "", f"Open for more than {backlog.settings['stale_action_days']} days.", ""]
    if not backlog.stale:
        return [*lines, "None.", ""]
    lines += ["| Item | Priority | Opened | Age (days) | Services |", "|---|---|---|---|---|"]
    for item in backlog.stale:
        age = (backlog.as_of - item.created).days
        where = ", ".join(_short(s) for s in item.services) or "not linked"
        lines.append(f"| {_item(item)} | {item.priority} | {item.created.isoformat()} | {age} | {where} |")
    return [*lines, ""]


def _list_part(title: str, entries: list[str]) -> list[str]:
    return [title, "", *(entries or ["None."]), ""]


def render_section(backlog: Backlog, level: int = 2) -> list[str]:
    """The backlog as markdown, headed at the given level."""
    h = "#" * level
    labels = ", ".join(f"`{label}`" for label in backlog.settings["action_labels"])
    lines = [
        f"{h} Recommendations backlog",
        "",
        f"Ranked by the sre-harness `trends.py` from {backlog.pirs_in_window} PIR(s) dated after "
        f"{backlog.window_start.isoformat()} up to {backlog.as_of.isoformat()}, and {backlog.items_read} issue(s) "
        f"labelled {labels}. The backlog is those issues; this ranks them.",
        "",
    ]
    if backlog.notes:
        lines += [*(f"- **Not complete:** {note}" for note in backlog.notes), ""]
    lines += _themes_part(backlog, h + "#") + _stale_part(backlog, h + "#")
    repeats = []
    for ref, count, open_items in backlog.repeats:
        repeats.append(f"- `{_short(ref)}`: {count} PIRs in the window, {len(open_items)} action item(s) still open:")
        repeats += [f"  - {_item(item)}" for item in open_items]
    lines += _list_part(f"{h}# Repeat incidents with open action items", repeats)
    due = [f"- `{_short(ref)}`: {'; '.join(why)}. Run the design-review skill." for ref, why in backlog.reviews_due]
    lines += _list_part(f"{h}# Design reviews due", due)
    loose = [f"- [{p.stem}]({p.url}) ({p.severity})" for p in backlog.unattributed]
    if loose:
        loose += ["", "- Add a `services:` line to their front matter, or catalogue what they affected."]
    lines += _list_part(f"{h}# Unattributed PIRs", loose)
    return lines


def as_json(backlog: Backlog) -> str:
    return json.dumps(asdict(backlog), indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--as-of", help="YYYY-MM-DD; default is today in the site's time zone")
    parser.add_argument("--pirs", help="directory of PIRs; default is the tracker repository's local checkout")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def action() -> int:
        site_root, doc = load_site(args.site)
        zone = ZoneInfo(doc["incident"]["timezone"])
        try:
            as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(zone).date()
        except ValueError as exc:
            raise SiteError(f"--as-of must be YYYY-MM-DD, not {args.as_of!r}") from exc
        backlog = build(site_root, doc, as_of, args.pirs)
        sys.stdout.write(as_json(backlog) + "\n" if args.json else "\n".join(render_section(backlog)).rstrip() + "\n")
        return 0

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
