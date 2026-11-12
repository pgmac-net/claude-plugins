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
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SITE_ENV = "SRE_HARNESS_SITE"
SITE_FILE = "site.yaml"

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[opsu]|github_pat)_[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Grafana token", re.compile(r"\bgl(?:c|sa)_[A-Za-z0-9+/=_-]{20,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("credentials in URL", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
)
OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9+=_-]{32,}$")
OPAQUE_ENTROPY_BITS = 4.0


class SiteError(Exception):
    """The site repository could not be found or read."""


@dataclass
class Report:
    """Outcome of validating one site."""

    site_root: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for --json output."""
        return {
            "site_root": str(self.site_root),
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "summary": self.summary,
        }


def _pointer_file(env: dict[str, str], home: Path) -> Path:
    config_home = env.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(config_home) / "sre-harness" / "site"


def resolve_site(
    explicit: str | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Find the site repository.

    Order: explicit path, the SRE_HARNESS_SITE environment variable, then
    ~/.config/sre-harness/site (a directory, a symlink to one, or a file whose
    first line is the path). Never guesses beyond that.
    """
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home

    candidate: Path | None = None
    source = ""
    if explicit:
        candidate, source = Path(explicit), "--site"
    elif env.get(SITE_ENV):
        candidate, source = Path(env[SITE_ENV]), SITE_ENV
    else:
        pointer = _pointer_file(env, home)
        if pointer.is_dir():
            candidate, source = pointer, str(pointer)
        elif pointer.is_file():
            lines = pointer.read_text(encoding="utf-8").strip().splitlines()
            first = lines[0].strip() if lines else ""
            if not first:
                raise SiteError(f"{pointer} is empty; it must contain the path to the site repository")
            candidate, source = Path(first), str(pointer)

    if candidate is None:
        raise SiteError(
            f"no site configured: set {SITE_ENV} to the site repository path, or create {_pointer_file(env, home)}"
        )

    root = candidate.expanduser()
    if not root.is_dir():
        raise SiteError(f"site path from {source} is not a directory: {root}")
    if not (root / SITE_FILE).is_file():
        raise SiteError(f"no {SITE_FILE} in site path from {source}: {root}")
    return root.resolve()


def load_yaml(path: Path) -> Any:
    """Parse one YAML file."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SiteError(f"{path}: not valid YAML: {exc}") from exc


# Validators whose jsonschema message names keys only, never values.
_KEY_ONLY_VALIDATORS = {"additionalProperties", "required"}
_SAFE_TEMPLATES = {
    "const": "must be {limit!r}",
    "enum": "must be one of {limit}",
    "pattern": "does not match the pattern {limit}",
    "type": "must be of type {limit}",
    "format": "is not a valid {limit}",
    "maxProperties": "must have at most {limit} properties",
    "minItems": "must have at least {limit} item(s)",
    "minLength": "must be at least {limit} character(s) long",
    "minimum": "must be at least {limit}",
    "uniqueItems": "must not contain duplicates",
}


def _safe_message(err: Any) -> str:
    """Describe a schema violation without echoing the offending value.

    jsonschema's own messages quote the instance, which would print a secret
    that was put in the wrong field.
    """
    if err.validator in _KEY_ONLY_VALIDATORS:
        return err.message
    template = _SAFE_TEMPLATES.get(err.validator)
    if template is None:
        return f"failed the '{err.validator}' check"
    return template.format(limit=err.validator_value)


def schema_errors(doc: Any, schema_name: str) -> list[str]:
    """Return human-readable schema violations for a document. Values are never echoed."""
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    found = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(part) for part in err.absolute_path) or "(root)"
        found.append(f"{where}: {_safe_message(err)}")
    return found


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


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
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                found.append(f"{path}: looks like a secret ({label}); reference it by environment variable name")
                break
        else:
            if OPAQUE_TOKEN.match(value) and _entropy(value) >= OPAQUE_ENTROPY_BITS:
                found.append(f"{path}: looks like a secret (opaque token); reference it by environment variable name")
    return found


def check_references(doc: dict[str, Any]) -> list[str]:
    """Repo aliases must resolve, and audit logs must go to a private repo."""
    errors = []
    repos = doc.get("repos", {})
    for section in ("tracker", "audit"):
        alias = doc.get(section, {}).get("repo")
        if alias is not None and alias not in repos:
            errors.append(f"{section}/repo: '{alias}' is not a key of repos")
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
    catalog_path = doc.get("catalog_path")
    if catalog_path and not (site_root / catalog_path).is_dir():
        errors.append(f"catalog_path: {catalog_path} is not a directory in the site repository")

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


def check_tiers(site_root: Path, doc: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Validate the tier definitions file named by site.yaml."""
    tiers_file = doc.get("tiers_file")
    if not tiers_file or not (site_root / tiers_file).is_file():
        return [], {}
    tiers_doc = load_yaml(site_root / tiers_file)
    errors = [f"{tiers_file}: {msg}" for msg in schema_errors(tiers_doc, "tiers.schema.json")]
    if errors:
        return errors, {}
    tiers = tiers_doc["tiers"]
    for key in ("id", "name"):
        for value, count in Counter(tier[key] for tier in tiers).items():
            if count > 1:
                errors.append(f"{tiers_file}: tier {key} '{value}' is used {count} times")
    return errors, {"tiers": [f"{tier['id']}:{tier['name']}" for tier in tiers]}


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
    tier_errors, tier_summary = check_tiers(site_root, doc)
    report.errors.extend(tier_errors)

    catalog_dir = site_root / doc["catalog_path"]
    entities = sorted(catalog_dir.rglob("*.yaml")) if catalog_dir.is_dir() else []
    report.summary = {
        "site": doc["site"]["name"],
        "repos": {alias: repo["visibility"] for alias, repo in doc["repos"].items()},
        "monitoring": [f"{src['name']} ({src['type']})" for src in doc["monitoring"]],
        "catalog_entities": len(entities),
        "audit": f"{doc['audit']['repo']}:{doc['audit']['path']}",
        "chat_configured": bool(doc.get("chat")),
        "slo_configured": bool(doc.get("slo")),
        **tier_summary,
    }
    return report


def _print_report(report: Report) -> None:
    print(f"site: {report.site_root}")
    for key, value in report.summary.items():
        print(f"  {key}: {value}")
    for warning in report.warnings:
        print(f"WARNING {warning}")
    for error in report.errors:
        print(f"ERROR {error}")
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
