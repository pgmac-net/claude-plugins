#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Validate a site's catalog and report drift between its two layers.

The generated layer (``<catalog>/generated/``) is written by catalog_sync.py.
Everything else under the catalog is written by people. This script checks
every entity against the schema, checks that references resolve, and reports
drift: Resources no service claims, services pointing at things that no
longer exist, and services missing what their tier requires.

It needs only the site repository, so it can run in that repository's CI.

Exit codes: 0 no errors, 1 errors, 2 the site could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sre_common import SITE_FILE, SiteError, load_yaml, print_findings, resolve_site, schema_errors

GENERATED_DIR = "generated"
GENERATED_ANNOTATION = "sre-harness/generated-by"
IGNORE_FILE = "ignore.yaml"
SOURCES_FILE = "sources.yaml"
REQUIREMENTS = ("dependencies", "pattern", "monitoring", "runbooks")


@dataclass
class Entity:
    """One catalog entity and where it came from."""

    rel: str
    doc: dict[str, Any]
    generated: bool

    @property
    def ref(self) -> str:
        return f"{self.doc['kind'].lower()}:{self.doc['metadata']['name']}"

    @property
    def spec(self) -> dict[str, Any]:
        return self.doc.get("spec") or {}


@dataclass
class CatalogReport:
    """Outcome of checking one catalog."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_entities(catalog_dir: Path, report: CatalogReport) -> list[Entity]:
    """Read and schema-check every entity file under the catalog."""
    entities = []
    for path in sorted(catalog_dir.rglob("*.yaml")):
        rel = path.relative_to(catalog_dir).as_posix()
        if rel in (IGNORE_FILE, f"{GENERATED_DIR}/{SOURCES_FILE}"):
            continue
        try:
            doc = load_yaml(path)
        except SiteError as exc:
            report.errors.append(str(exc))
            continue
        problems = schema_errors(doc, "entity.schema.json")
        if problems:
            report.errors.extend(f"{rel}: {msg}" for msg in problems)
            continue
        in_generated = rel.startswith(f"{GENERATED_DIR}/")
        marked = GENERATED_ANNOTATION in (doc["metadata"].get("annotations") or {})
        if in_generated and not marked:
            report.errors.append(
                f"{rel}: hand-written entity inside {GENERATED_DIR}/; move it out, sync would delete it"
            )
        if marked and not in_generated:
            report.errors.append(f"{rel}: generated entity outside {GENERATED_DIR}/; remove the copy or its annotation")
        entities.append(Entity(rel=rel, doc=doc, generated=in_generated))
    return entities


def _refs(entity: Entity) -> list[tuple[str, str]]:
    """Every (field, ref) an entity points at."""
    spec = entity.spec
    found = [("dependsOn", ref) for ref in spec.get("dependsOn", [])]
    if spec.get("owner"):
        found.append(("owner", spec["owner"]))
    if spec.get("system"):
        system = spec["system"]
        found.append(("system", system if ":" in system else f"system:{system}"))
    return found


def check_references(entities: list[Entity], report: CatalogReport) -> dict[str, Entity]:
    """Entity refs must be unique, and every reference must resolve."""
    by_ref: dict[str, Entity] = {}
    for entity in entities:
        if entity.ref in by_ref:
            report.errors.append(f"{entity.rel}: {entity.ref} is already defined in {by_ref[entity.ref].rel}")
        else:
            by_ref[entity.ref] = entity
    dangling: dict[tuple[str, str], list[str]] = {}
    for entity in entities:
        for field_name, ref in _refs(entity):
            if ref not in by_ref:
                dangling.setdefault((field_name, ref), []).append(entity.rel)
    for (field_name, ref), rels in sorted(dangling.items()):
        where = rels[0] if len(rels) == 1 else f"{len(rels)} entities (first: {rels[0]})"
        report.errors.append(f"{where}: {field_name} points at {ref}, which does not exist (dangling)")
    return by_ref


def _has_monitoring(entity: Entity, by_ref: dict[str, Entity]) -> bool:
    if entity.spec.get("sre", {}).get("monitoring"):
        return True
    for ref in entity.spec.get("dependsOn", []):
        target = by_ref.get(ref)
        if target and (target.spec.get("sre") or {}).get("monitoring", {}).get("checks"):
            return True
    return False


def _check_service(
    entity: Entity, by_ref: dict[str, Entity], tier: dict[str, Any], site: dict[str, Any], report: CatalogReport
) -> None:
    sre = entity.spec["sre"]
    present = {
        "dependencies": bool(entity.spec.get("dependsOn")),
        "pattern": bool(sre.get("pattern")),
        "monitoring": _has_monitoring(entity, by_ref),
        "runbooks": bool(sre.get("runbooks")),
    }
    unverified = set(sre.get("unverified", []))
    required = set(tier.get("catalog_requires", []))
    for item in (i for i in REQUIREMENTS if i in required and not present[i]):
        if item in unverified:
            report.warnings.append(
                f"{entity.rel}: {item} required at tier {sre['tier']} but not yet known (unverified)"
            )
        else:
            report.errors.append(f"{entity.rel}: {item} required at tier {sre['tier']} but missing")
    drafted = sorted(item for item in unverified if present.get(item, True))
    if drafted:
        report.warnings.append(f"{entity.rel}: drafted but not confirmed by a person: {', '.join(drafted)}")
    if not present["pattern"] and "pattern" not in required:
        report.warnings.append(f"{entity.rel}: no deployment pattern stated")

    patterns_dir = site.get("patterns_dir")
    if sre.get("pattern") and patterns_dir and not (patterns_dir / f"{sre['pattern']}.md").is_file():
        report.errors.append(f"{entity.rel}: pattern '{sre['pattern']}' has no page in the patterns directory")
    runbooks_dir = site.get("runbooks_dir")
    if runbooks_dir and runbooks_dir.is_dir():
        for runbook in sre.get("runbooks", []):
            if not (runbooks_dir / runbook).is_file():
                report.warnings.append(f"{entity.rel}: runbook not found: {runbook}")


def check_services(
    entities: list[Entity],
    by_ref: dict[str, Entity],
    tiers: dict[int, dict[str, Any]],
    site: dict[str, Any],
    report: CatalogReport,
) -> None:
    """Each service must state what its tier requires, or say that it is unverified."""
    for entity in entities:
        if entity.doc["kind"] != "Component":
            continue
        tier = tiers.get(entity.spec["sre"]["tier"])
        if tier is None:
            report.errors.append(
                f"{entity.rel}: tier {entity.spec['sre']['tier']} is not defined in the tier definitions"
            )
        else:
            _check_service(entity, by_ref, tier, site, report)


def check_coverage(entities: list[Entity], by_ref: dict[str, Entity], catalog_dir: Path, report: CatalogReport) -> int:
    """Generated Resources that no service reaches are uncatalogued, unless deliberately ignored."""
    reached: set[str] = set()
    frontier = [e.ref for e in entities if e.doc["kind"] == "Component"]
    while frontier:
        ref = frontier.pop()
        for target in by_ref[ref].spec.get("dependsOn", []) if ref in by_ref else []:
            if target not in reached:
                reached.add(target)
                frontier.append(target)

    ignored: dict[str, str] = {}
    ignore_path = catalog_dir / IGNORE_FILE
    if ignore_path.is_file():
        doc = load_yaml(ignore_path) or {}
        entries = doc.get("ignore") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            report.errors.append(f"{IGNORE_FILE}: must be a mapping with an 'ignore' list")
            entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("ref") or not str(entry.get("reason", "")).strip():
                report.errors.append(f"{IGNORE_FILE}: every entry needs a 'ref' and a 'reason'")
                continue
            ignored[entry["ref"]] = entry["reason"]
    for ref in sorted(ignored):
        if ref not in by_ref:
            report.warnings.append(f"{IGNORE_FILE}: {ref} no longer exists; remove the entry")
        elif ref in reached:
            report.warnings.append(f"{IGNORE_FILE}: {ref} is now part of a service; remove the entry")

    uncatalogued = sorted(e.ref for e in entities if e.generated and e.ref not in reached and e.ref not in ignored)
    for ref in uncatalogued:
        report.warnings.append(f"uncatalogued: {ref} belongs to no service and is not in {IGNORE_FILE}")
    return len(uncatalogued)


def check(site_root: Path, site_doc: dict[str, Any], tiers_doc: dict[str, Any] | None) -> CatalogReport:
    """Run every catalog check for one site. Expects a schema-valid site.yaml."""
    report = CatalogReport()
    catalog = site_doc["catalog"]
    catalog_dir = site_root / catalog["path"]
    if not catalog_dir.is_dir():
        report.errors.append(f"catalog path {catalog['path']} is not a directory")
        return report

    context: dict[str, Any] = {}
    if site_doc.get("patterns_path"):
        context["patterns_dir"] = site_root / site_doc["patterns_path"]
    repo = site_doc["repos"].get(catalog.get("runbooks_repo", ""), {})
    if repo.get("path") and repo.get("runbooks_path"):
        context["runbooks_dir"] = Path(repo["path"]).expanduser() / repo["runbooks_path"]

    entities = load_entities(catalog_dir, report)
    by_ref = check_references(entities, report)
    tiers = {tier["id"]: tier for tier in (tiers_doc or {}).get("tiers", [])}
    check_services(entities, by_ref, tiers, context, report)
    uncatalogued = check_coverage(entities, by_ref, catalog_dir, report)

    services = [e for e in entities if e.doc["kind"] == "Component"]
    by_tier: dict[str, int] = {}
    for service in services:
        key = str(service.spec["sre"]["tier"])
        by_tier[key] = by_tier.get(key, 0) + 1
    report.summary = {
        "services": len(services),
        "services_by_tier": dict(sorted(by_tier.items())),
        "systems": sum(1 for e in entities if e.doc["kind"] == "System"),
        "generated_resources": sum(1 for e in entities if e.generated),
        "uncatalogued": uncatalogued,
        "unverified_services": sum(1 for s in services if s.spec["sre"].get("unverified")),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--site", help="site repository path (default: SRE_HARNESS_SITE, then ~/.config/sre-harness/site)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        site_root = resolve_site(args.site)
        site_doc = load_yaml(site_root / SITE_FILE)
        problems = schema_errors(site_doc, "site.schema.json")
        if problems:
            raise SiteError(f"site.yaml is not valid ({len(problems)} problem(s)); run validate_site.py first")
        tiers_path = site_root / site_doc["tiers_file"]
        tiers_doc = load_yaml(tiers_path) if tiers_path.is_file() else None
        report = check(site_root, site_doc, tiers_doc)
    except SiteError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {"ok": report.ok, "errors": report.errors, "warnings": report.warnings, "summary": report.summary},
                indent=2,
            )
        )
    else:
        print_findings(report.summary, report.warnings, report.errors)
        print("catalog ok" if report.ok else f"catalog invalid: {len(report.errors)} error(s)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
