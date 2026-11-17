#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Check, draw and list findings from STPA analyses in the site repository.

An analysis is a markdown file, <analyses_path>/<service>.md, whose YAML front
matter holds the STPA steps: losses and hazards, the control structure
(controllers, controlled processes, control actions, feedback), unsafe control
actions (UCAs) and loss scenarios. The design-review skill writes it; this
script checks it and keeps its diagram in step.

  check     schema, identifiers, cross-references and evidence rules for every analysis
  render    rewrite the Mermaid control-structure diagram in one analysis's body
  findings  the loss scenarios with no working control, and the issues that track them

Exit codes: 0 clean, 1 problems found, 2 the site could not be used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import catalog_check
import yaml
from sre_common import SiteError, guarded, load_site, print_findings, schema_errors

DEFAULTS: dict[str, Any] = {
    "analyses_path": "analyses/stpa",
    "theme_min_pirs": 3,
    "theme_window_days": 180,
    "stale_action_days": 60,
    "action_labels": ["incident", "design-review"],
    "ignore_tags": [],
}
DIAGRAM_START = "<!-- stpa:diagram start (stpa.py render rewrites this block) -->"
DIAGRAM_END = "<!-- stpa:diagram end -->"
# Lists whose entries carry an id, and the prefix each id must have.
ID_LISTS = {
    "losses": "L",
    "hazards": "H",
    "controllers": "C",
    "processes": "P",
    "control_actions": "CA",
    "feedback": "F",
    "ucas": "UCA",
    "scenarios": "LS",
}


def settings(doc: dict[str, Any]) -> dict[str, Any]:
    """The site's reliability settings, with every default filled in."""
    return {**DEFAULTS, **(doc.get("reliability") or {})}


@dataclass
class Analysis:
    path: Path
    rel: str
    meta: dict[str, Any]
    body: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def service(self) -> str:
        return self.meta.get("service", "")

    @property
    def reviewed(self) -> date | None:
        try:
            return date.fromisoformat(str(self.meta.get("reviewed")))
        except ValueError:
            return None


class _StringDates(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """A safe loader that leaves dates as strings, so the schema sees what was written."""


_StringDates.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """The YAML front matter and the body after it. Dates are kept as ISO strings."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    try:
        loaded = yaml.load(text[4:end], Loader=_StringDates)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        return None, text
    after = text.find("\n", end + 4)
    return (loaded if isinstance(loaded, dict) else None), (text[after + 1 :] if after >= 0 else "")


# --- checking ---------------------------------------------------------------------------


def _ids(meta: dict[str, Any], key: str) -> set[str]:
    return {item.get("id") for item in meta.get(key) or [] if isinstance(item, dict)}


def _check_ids(meta: dict[str, Any], errors: list[str]) -> None:
    seen: set[str] = set()
    for key in ID_LISTS:
        for item in meta.get(key) or []:
            ident = item.get("id")
            if ident in seen:
                errors.append(f"{key}: id {ident} is used more than once")
            seen.add(ident)


def _check_refs(meta: dict[str, Any], errors: list[str]) -> None:
    losses, hazards = _ids(meta, "losses"), _ids(meta, "hazards")
    controllers, processes = _ids(meta, "controllers"), _ids(meta, "processes")
    actions, ucas = _ids(meta, "control_actions"), _ids(meta, "ucas")
    nodes = controllers | processes

    def need(where: str, refs: list[str], known: set[str], what: str) -> None:
        for ref in refs:
            if ref not in known:
                errors.append(f"{where}: {ref} is not a defined {what}")

    for item in meta.get("hazards") or []:
        need(item["id"], item["losses"], losses, "loss")
    for item in meta.get("control_actions") or []:
        need(item["id"], [item["from"]], controllers, "controller")
        need(item["id"], [item["to"]], nodes, "controller or process")
    for item in meta.get("feedback") or []:
        need(item["id"], [item["from"]], nodes, "controller or process")
        need(item["id"], [item["to"]], controllers, "controller")
    for item in meta.get("ucas") or []:
        need(item["id"], [item["action"]], actions, "control action")
        need(item["id"], item["hazards"], hazards, "hazard")
    for item in meta.get("scenarios") or []:
        need(item["id"], item["ucas"], ucas, "unsafe control action")


def _check_scenarios(meta: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    reviewed = meta.get("status") == "reviewed"
    for item in meta.get("scenarios") or []:
        if item["status"] == "controlled" and not item.get("control"):
            errors.append(f"{item['id']}: a controlled scenario must name its control")
        if item["status"] == "uncontrolled" and not item.get("finding"):
            (errors if reviewed else warnings).append(f"{item['id']}: uncontrolled and no finding issue linked yet")
        if item["status"] == "controlled" and item.get("finding"):
            warnings.append(f"{item['id']}: controlled but still links a finding; say why or remove it")


def _check_rules(meta: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    """Evidence, controls and coverage: what makes an analysis honest rather than merely valid."""
    for item in meta.get("ucas") or []:
        if bool(item.get("evidence")) == bool(item.get("hypothesised")):
            errors.append(f"{item['id']}: cite evidence or mark it hypothesised: true, not both and not neither")
    _check_scenarios(meta, errors, warnings)
    used_actions = {item["action"] for item in meta.get("ucas") or []}
    warnings.extend(
        f"{item['id']}: no unsafe control action considered for '{item['action']}'"
        for item in meta.get("control_actions") or []
        if item["id"] not in used_actions
    )
    used_hazards = {ref for item in meta.get("ucas") or [] for ref in item["hazards"]}
    warnings.extend(
        f"{item['id']}: no unsafe control action leads to this hazard"
        for item in meta.get("hazards") or []
        if item["id"] not in used_hazards
    )
    fed = {item["to"] for item in meta.get("feedback") or [] if item["status"] != "missing"}
    warnings.extend(
        f"{item['id']}: automated controller '{item['name']}' receives no working feedback"
        for item in meta.get("controllers") or []
        if item["kind"] == "automated" and item["id"] not in fed
    )
    if meta.get("proposed"):
        warnings.append(f"{len(meta['proposed'])} proposed change(s) from PIRs wait for review")


def check_analysis(analysis: Analysis, services: set[str] | None) -> None:
    meta = analysis.meta
    problems = schema_errors(meta, "stpa.schema.json")
    if problems:
        analysis.errors.extend(problems)
        return
    name = meta["service"].split(":", 1)[1]
    if analysis.path.stem != name:
        analysis.errors.append(f"file is named {analysis.path.name} but analyses {meta['service']}; name it {name}.md")
    if services is not None and meta["service"] not in services:
        analysis.errors.append(f"{meta['service']} is not in the catalog")
    _check_ids(meta, analysis.errors)
    _check_refs(meta, analysis.errors)
    _check_rules(meta, analysis.errors, analysis.warnings)
    if DIAGRAM_START in analysis.body and diagram_block(meta) not in analysis.body:
        analysis.warnings.append("diagram is out of date; run stpa.py render")


def analyses_dir(site_root: Path, doc: dict[str, Any]) -> Path:
    return site_root / settings(doc)["analyses_path"]


def load_analyses(site_root: Path, doc: dict[str, Any], services: set[str] | None = None) -> list[Analysis]:
    """Every analysis in the site, each checked. A file that cannot be parsed comes back with an error."""
    folder = analyses_dir(site_root, doc)
    out = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if path.name.lower() == "readme.md":
            continue
        rel = str(path.relative_to(site_root))
        meta, body = split_front_matter(path.read_text(encoding="utf-8"))
        analysis = Analysis(path, rel, meta or {}, body)
        if meta is None:
            analysis.errors.append("no YAML front matter")
        else:
            check_analysis(analysis, services)
        out.append(analysis)
    return out


def catalog_services(site_root: Path, doc: dict[str, Any]) -> set[str]:
    report = catalog_check.CatalogReport()
    entities = catalog_check.load_entities(site_root / doc["catalog"]["path"], report)
    return {entity.ref for entity in entities if entity.doc["kind"] == "Component"}


# --- the diagram ----------------------------------------------------------------------------


def _label(text: str) -> str:
    return text.replace('"', "'").replace("|", "/")


def diagram(meta: dict[str, Any]) -> str:
    """A Mermaid flowchart: controllers above, processes as rounded nodes, feedback dashed, missing feedback red."""
    lines = ["flowchart TB"]
    for item in meta["controllers"]:
        lines.append(f'  {item["id"].replace("-", "")}["{item["id"]} {_label(item["name"])}<br/>({item["kind"]})"]')
    for item in meta["processes"]:
        lines.append(f'  {item["id"].replace("-", "")}("{item["id"]} {_label(item["name"])}")')
    red = []
    edges = 0
    for item in meta["control_actions"]:
        src, dst = item["from"].replace("-", ""), item["to"].replace("-", "")
        lines.append(f'  {src} -->|"{item["id"]}: {_label(item["action"])}"| {dst}')
        edges += 1
    for item in meta.get("feedback") or []:
        src, dst = item["from"].replace("-", ""), item["to"].replace("-", "")
        mark = "" if item["status"] == "present" else f" ({item['status'].upper()})"
        lines.append(f'  {src} -.->|"{item["id"]}: {_label(item["signal"])}{mark}"| {dst}')
        if item["status"] != "present":
            red.append(edges)
        edges += 1
    if red:
        lines.append(f"  linkStyle {','.join(str(n) for n in red)} stroke:#d93f0b,stroke-width:2px")
    return "\n".join(lines)


def diagram_block(meta: dict[str, Any]) -> str:
    return f"{DIAGRAM_START}\n```mermaid\n{diagram(meta)}\n```\n{DIAGRAM_END}"


def render(analysis: Analysis) -> bool:
    """Rewrite the diagram block in place, adding one after the first heading if there is none. True if changed."""
    text = analysis.path.read_text(encoding="utf-8")
    block = diagram_block(analysis.meta)
    pattern = re.compile(re.escape(DIAGRAM_START) + r".*?" + re.escape(DIAGRAM_END), re.DOTALL)
    if pattern.search(text):
        new = pattern.sub(lambda _: block, text, count=1)
    else:
        head_end = len(text) - len(analysis.body)
        new = text[:head_end] + analysis.body.rstrip() + f"\n\n## Control structure\n\n{block}\n"
    if new == text:
        return False
    analysis.path.write_text(new, encoding="utf-8")
    return True


# --- findings ----------------------------------------------------------------------------------


def findings(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Uncontrolled loss scenarios, each with its UCAs, hazards and the issue that tracks it (or None)."""
    ucas = {item["id"]: item for item in meta.get("ucas") or []}
    out = []
    for item in meta.get("scenarios") or []:
        if item["status"] != "uncontrolled":
            continue
        linked = [ucas[ref] for ref in item["ucas"] if ref in ucas]
        out.append(
            {
                "id": item["id"],
                "text": item["text"],
                "ucas": item["ucas"],
                "hazards": sorted({h for uca in linked for h in uca["hazards"]}),
                "causal_factors": item.get("causal_factors", []),
                "finding": item.get("finding"),
            }
        )
    return out


# --- commands -----------------------------------------------------------------------------------


def _pick(site_root: Path, doc: dict[str, Any], service: str) -> Analysis:
    name = service.split(":", 1)[-1]
    for analysis in load_analyses(site_root, doc):
        if analysis.path.stem == name:
            return analysis
    raise SiteError(f"no analysis for {name} in {settings(doc)['analyses_path']}")


def cmd_check(args: argparse.Namespace) -> int:
    site_root, doc = load_site(args.site)
    found = load_analyses(site_root, doc, catalog_services(site_root, doc))
    errors = [f"{a.rel}: {e}" for a in found for e in a.errors]
    warnings = [f"{a.rel}: {w}" for a in found for w in a.warnings]
    summary = {
        "analyses": len(found),
        "reviewed": sum(a.meta.get("status") == "reviewed" for a in found),
        "uncontrolled scenarios": sum(len(findings(a.meta)) for a in found if not a.errors),
    }
    if args.json:
        print(json.dumps({"summary": summary, "warnings": warnings, "errors": errors}, indent=2))
    else:
        print_findings(summary, warnings, errors)
    return 1 if errors else 0


def cmd_render(args: argparse.Namespace) -> int:
    site_root, doc = load_site(args.site)
    analysis = _pick(site_root, doc, args.service)
    if analysis.errors:
        print_findings({}, [], [f"{analysis.rel}: {e}" for e in analysis.errors])
        return 1
    print(f"{'rewrote' if render(analysis) else 'unchanged'} {analysis.rel}")
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    site_root, doc = load_site(args.site)
    analysis = _pick(site_root, doc, args.service)
    if analysis.errors:
        print_findings({}, [], [f"{analysis.rel}: {e}" for e in analysis.errors])
        return 1
    found = findings(analysis.meta)
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    for item in found:
        tracked = item["finding"] or "NOT FILED"
        print(f"{item['id']} [{', '.join(item['hazards'])}] {item['text']}\n  issue: {tracked}")
    if not found:
        print("no uncontrolled loss scenarios")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check every analysis")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)
    draw = sub.add_parser("render", help="rewrite one analysis's diagram")
    draw.add_argument("service", help="service name or component:<name>")
    draw.set_defaults(func=cmd_render)
    find = sub.add_parser("findings", help="uncontrolled loss scenarios of one analysis")
    find.add_argument("service", help="service name or component:<name>")
    find.add_argument("--json", action="store_true")
    find.set_defaults(func=cmd_findings)
    args = parser.parse_args(argv)
    return guarded(lambda: args.func(args))


if __name__ == "__main__":
    sys.exit(main())
