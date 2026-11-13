"""Shared helpers for the sre-harness scripts: finding the site, loading files, schema checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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


# --- incidents ---------------------------------------------------------------

SEVERITIES = ("P1", "P2", "P3", "P4")
INCIDENT_ID = re.compile(r"^([a-z][a-z0-9_-]*)-(\d+)$")


def state_dir(env: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """Per-user directory for incident state that must not live in a repository."""
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home
    base = env.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "sre-harness"


def load_site(explicit: str | None = None) -> tuple[Path, dict[str, Any]]:
    """Resolve the site and return its root and a schema-valid site document."""
    site_root = resolve_site(explicit)
    doc = load_yaml(site_root / SITE_FILE)
    problems = schema_errors(doc, "site.schema.json")
    if problems:
        raise SiteError(f"site.yaml is not valid ({len(problems)} problem(s)); run validate_site.py first")
    return site_root, doc


def split_incident_id(incident_id: str, doc: dict[str, Any]) -> tuple[str, int]:
    """Turn '<repo-alias>-<number>' into the code-host repository and issue number."""
    match = INCIDENT_ID.match(incident_id)
    if not match:
        raise SiteError(f"'{incident_id}' is not an incident id; expected <repo-alias>-<number>")
    alias, number = match.group(1), int(match.group(2))
    repo = doc["repos"].get(alias)
    if repo is None:
        raise SiteError(f"incident id '{incident_id}': repo alias '{alias}' is not in repos")
    return repo["repo"], number


def run_gh(args: list[str], stdin: str | None = None) -> str:
    """Run the gh CLI and return its stdout. Raises SiteError with gh's own message on failure."""
    gh = shutil.which("gh")
    if not gh:
        raise SiteError("the gh CLI is not installed")
    done = subprocess.run([gh, *args], input=stdin, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        raise SiteError(f"gh {' '.join(args[:2])} failed: {lines[-1] if lines else 'no output'}")
    return done.stdout


def utc_now() -> datetime:
    """Current time, second precision, in UTC."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_utc(text: str) -> datetime:
    """Parse an ISO 8601 time. A time with no offset is taken as UTC."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).replace(microsecond=0)


def iso_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a 'Z' suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_clock(moment: datetime, timezone: str) -> str:
    """'HH:MM ZZZ' in the site's time zone, for lines people read."""
    return moment.astimezone(ZoneInfo(timezone)).strftime("%H:%M %Z")


def incident_state_path(incident_id: str) -> Path:
    """Where one incident's local state is kept."""
    return state_dir() / "incidents" / f"{incident_id}.json"


def read_incident_state(incident_id: str | None = None) -> dict[str, Any]:
    """Load local state for an incident, or for the current one when no id is given."""
    if incident_id is None:
        pointer = state_dir() / "current"
        if not pointer.is_file():
            raise SiteError("no current incident; open one with incident.py open, or pass --incident")
        incident_id = pointer.read_text(encoding="utf-8").strip()
    path = incident_state_path(incident_id)
    if not path.is_file():
        raise SiteError(f"no local state for incident '{incident_id}'; run incident.py attach {incident_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_incident_state(state: dict[str, Any], make_current: bool = True) -> None:
    """Persist local state for an incident and, by default, make it the current one."""
    path = incident_state_path(state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    if make_current:
        (state_dir() / "current").write_text(state["id"] + "\n", encoding="utf-8")


def guarded(action: Callable[[], int]) -> int:
    """Run a command body; a SiteError becomes a message on stderr and exit code 2."""
    try:
        return action()
    except SiteError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
