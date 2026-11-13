#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Validate an sre-harness site repository.

Checks site.yaml and the tier definitions against the plugin schemas, then
applies the rules a schema cannot express: referenced paths exist, repo
aliases resolve, audit logs land in a private repository, and no value looks
like a secret.

Exit codes: 0 valid, 1 invalid, 2 no site could be resolved.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import catalog_check
from redact import secret_label
from sre_common import SITE_ENV, SITE_FILE, Report, SiteError, load_yaml, print_findings, resolve_site, schema_errors


def _walk_strings(node: Any, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}/{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}/{index}")
    elif isinstance(node, str):
        yield path, node


def find_secrets(doc: Any) -> list[str]:
    """Flag values that look like secrets. Values are never echoed."""
    found = []
    for path, value in _walk_strings(doc):
        label = secret_label(value)
        if label:
            found.append(f"{path}: looks like a secret ({label}); reference it by environment variable name")
    return found


def check_references(doc: dict[str, Any]) -> list[str]:
    """Repo aliases must resolve, and audit logs must go to a private repo."""
    errors = []
    repos = doc.get("repos", {})
    for section in ("tracker", "audit"):
        alias = doc.get(section, {}).get("repo")
        if alias is not None and alias not in repos:
            errors.append(f"{section}/repo: '{alias}' is not a key of repos")
    catalog = doc.get("catalog", {})
    runbooks_repo = catalog.get("runbooks_repo")
    if runbooks_repo is not None and runbooks_repo not in repos:
        errors.append(f"catalog/runbooks_repo: '{runbooks_repo}' is not a key of repos")
    for name, count in Counter(source["name"] for source in catalog.get("sources", [])).items():
        if count > 1:
            errors.append(f"catalog/sources: source name '{name}' is used {count} times")
    for source in catalog.get("sources", []):
        alias = source["repo"]
        if alias not in repos:
            errors.append(f"catalog/sources/{source['name']}/repo: '{alias}' is not a key of repos")
        elif alias != "catalog" and not repos[alias].get("path"):
            errors.append(f"catalog/sources/{source['name']}/repo: repos/{alias} has no local path to read from")
    audit_alias = doc.get("audit", {}).get("repo")
    if audit_alias in repos and repos[audit_alias].get("visibility") != "private":
        errors.append(
            f"audit/repo: '{audit_alias}' is not private; audit logs record every command "
            "and must not be written to a public repository"
        )
    names = [source.get("name") for source in doc.get("monitoring", [])]
    for name, count in Counter(names).items():
        if count > 1:
            errors.append(f"monitoring: source name '{name}' is used {count} times")
    return errors


def check_paths(site_root: Path, doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Paths inside the site repo must exist; missing external checkouts only warn."""
    errors, warnings = [], []
    tiers_file = doc.get("tiers_file")
    if tiers_file and not (site_root / tiers_file).is_file():
        errors.append(f"tiers_file: {tiers_file} does not exist in the site repository")
    catalog_path = doc.get("catalog", {}).get("path")
    if catalog_path and not (site_root / catalog_path).is_dir():
        errors.append(f"catalog/path: {catalog_path} is not a directory in the site repository")
    patterns_path = doc.get("patterns_path")
    if patterns_path and not (site_root / patterns_path).is_dir():
        errors.append(f"patterns_path: {patterns_path} is not a directory in the site repository")

    repos = doc.get("repos", {})
    audit = doc.get("audit", {})
    if audit.get("repo") == "catalog" and audit.get("path"):
        if not (site_root / audit["path"]).is_dir():
            errors.append(f"audit/path: {audit['path']} is not a directory in the site repository")

    for alias, repo in repos.items():
        if alias == "catalog":
            if "path" in repo:
                warnings.append("repos/catalog/path: ignored; the catalog repo is the site root")
            continue
        local = repo.get("path")
        if not local:
            continue
        checkout = Path(local).expanduser()
        if not checkout.is_dir():
            warnings.append(f"repos/{alias}/path: local checkout not found: {local}")
            continue
        for key in ("runbooks_path", "pirs_path", "templates_path"):
            if repo.get(key) and not (checkout / repo[key]).exists():
                warnings.append(f"repos/{alias}/{key}: {repo[key]} not found in {local}")
    return errors, warnings


def check_incident(site_root: Path, doc: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The incident section must name a real time zone, existing procedures and a private drill tracker."""
    errors = []
    incident = doc.get("incident", {})
    repos = doc.get("repos", {})
    try:
        ZoneInfo(incident.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("incident/timezone: not a known IANA time zone")

    procedures_dir = site_root / incident.get("procedures_path", "")
    if not procedures_dir.is_dir():
        errors.append(
            f"incident/procedures_path: {incident.get('procedures_path')} is not a directory in the site repository"
        )
    else:
        for stage, files in incident.get("procedures", {}).items():
            for name in files:
                if not (procedures_dir / name).is_file():
                    errors.append(f"incident/procedures/{stage}: {name} not found in {incident['procedures_path']}")

    criteria = incident.get("severity_criteria")
    if criteria and criteria["repo"] not in repos:
        errors.append(f"incident/severity_criteria/repo: '{criteria['repo']}' is not a key of repos")
    drill = incident.get("drill")
    if drill:
        if drill["repo"] not in repos:
            errors.append(f"incident/drill/repo: '{drill['repo']}' is not a key of repos")
        elif repos[drill["repo"]].get("visibility") != "private":
            errors.append(
                f"incident/drill/repo: '{drill['repo']}' is not private; practice incidents must not be published"
            )
    autonomy_file = incident.get("autonomy_file")
    if autonomy_file and not (site_root / autonomy_file).is_file():
        errors.append(f"incident/autonomy_file: {autonomy_file} does not exist in the site repository")
    assist = incident.get("assist")
    if assist and assist["enabled"] and not assist.get("api_key_env"):
        errors.append("incident/assist: enabled but no api_key_env names where the key is held")

    summary = {
        "timezone": incident.get("timezone"),
        "update_minutes": incident.get("update_minutes", {}),
        "procedures": {stage: len(files) for stage, files in incident.get("procedures", {}).items()},
        "drill": f"{drill['repo']} ({drill['label']})" if drill else None,
        "assist": assist["provider"] if assist and assist["enabled"] else None,
    }
    return errors, summary


def check_tiers(site_root: Path, doc: dict[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, Any] | None]:
    """Validate the tier definitions file named by site.yaml. Returns errors, summary and the document."""
    tiers_file = doc.get("tiers_file")
    if not tiers_file or not (site_root / tiers_file).is_file():
        return [], {}, None
    tiers_doc = load_yaml(site_root / tiers_file)
    errors = [f"{tiers_file}: {msg}" for msg in schema_errors(tiers_doc, "tiers.schema.json")]
    if errors:
        return errors, {}, None
    tiers = tiers_doc["tiers"]
    for key in ("id", "name"):
        for value, count in Counter(tier[key] for tier in tiers).items():
            if count > 1:
                errors.append(f"{tiers_file}: tier {key} '{value}' is used {count} times")
    return errors, {"tiers": [f"{tier['id']}:{tier['name']}" for tier in tiers]}, tiers_doc


def validate(site_root: Path) -> Report:
    """Run every check against one site repository."""
    report = Report(site_root=site_root)
    doc = load_yaml(site_root / SITE_FILE)
    report.errors.extend(f"{SITE_FILE}: {msg}" for msg in schema_errors(doc, "site.schema.json"))
    if not isinstance(doc, dict):
        return report

    report.errors.extend(f"{SITE_FILE}: {msg}" for msg in find_secrets(doc))
    if report.errors:
        # Later checks assume a schema-valid document.
        return report

    report.errors.extend(f"{SITE_FILE}: {msg}" for msg in check_references(doc))
    path_errors, path_warnings = check_paths(site_root, doc)
    report.errors.extend(f"{SITE_FILE}: {msg}" for msg in path_errors)
    report.warnings.extend(f"{SITE_FILE}: {msg}" for msg in path_warnings)
    tier_errors, tier_summary, tiers_doc = check_tiers(site_root, doc)
    report.errors.extend(tier_errors)
    incident_errors, incident_summary = check_incident(site_root, doc)
    report.errors.extend(f"{SITE_FILE}: {msg}" for msg in incident_errors)

    catalog_summary: dict[str, Any] = {}
    if not path_errors and not tier_errors:
        catalog_report = catalog_check.check(site_root, doc, tiers_doc)
        report.errors.extend(f"catalog: {msg}" for msg in catalog_report.errors)
        report.warnings.extend(f"catalog: {msg}" for msg in catalog_report.warnings)
        catalog_summary = catalog_report.summary
    report.summary = {
        "site": doc["site"]["name"],
        "repos": {alias: repo["visibility"] for alias, repo in doc["repos"].items()},
        "monitoring": [f"{src['name']} ({src['type']})" for src in doc["monitoring"]],
        "catalog_sources": [f"{src['name']} ({src['type']})" for src in doc["catalog"].get("sources", [])],
        "catalog": catalog_summary,
        "audit": f"{doc['audit']['repo']}:{doc['audit']['path']}",
        "incident": incident_summary,
        "chat_configured": bool(doc.get("chat")),
        "slo_configured": bool(doc.get("slo")),
        **tier_summary,
    }
    return report


def _print_report(report: Report) -> None:
    print(f"site: {report.site_root}")
    print_findings(report.summary, report.warnings, report.errors)
    print("valid" if report.ok else f"invalid: {len(report.errors)} error(s)")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help=f"site repository path (default: {SITE_ENV}, then ~/.config/sre-harness/site)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        site_root = resolve_site(args.site)
        report = validate(site_root)
    except SiteError as exc:
        if args.json:
            print(json.dumps({"ok": False, "errors": [str(exc)]}))
        else:
            print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
