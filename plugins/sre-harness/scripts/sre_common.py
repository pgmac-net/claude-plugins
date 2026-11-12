"""Shared helpers for the sre-harness scripts: finding the site, loading files, schema checks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SITE_ENV = "SRE_HARNESS_SITE"
SITE_FILE = "site.yaml"


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


def print_findings(summary: dict[str, Any], warnings: list[str], errors: list[str]) -> None:
    """Print a report body in the order every script uses: summary, warnings, errors."""
    for key, value in summary.items():
        print(f"  {key}: {value}")
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(f"ERROR {error}")
