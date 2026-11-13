#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Decide whether a command is a pre-approved (Tier 2) action.

The decision is made here, by exact matching against the site's allow-list,
not by the agent's judgment. A command is Tier 2 only if it is one simple
command, token for token equal to a listed template, with every variable part
satisfying that template's rule, for a service that is not suspended, and
within the action's limit for this incident. Everything else is Tier 1: stop
and ask.

Commands:
  match   say which tier a command is; changes nothing
  claim   match, and if Tier 2, record the claim in the audit log so that the
          limit is enforced and the review can see it
  list    show the allow-list

Exit codes: 0 always for a decision (read 'tier' in the output), 2 the site
or the allow-list could not be used.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import audit_hook
import redact
import sre_common
from sre_common import SiteError, guarded, load_site, load_yaml, schema_errors

SHELL_OPERATORS = re.compile(r"[;&|<>`\n\r]|\$\(|\$\{|\$[A-Za-z_]")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def load_allow_list(site_root: Path, doc: dict[str, Any]) -> dict[str, Any]:
    """The site's allow-list, or an empty disabled one when the site has none."""
    name = doc["incident"].get("autonomy_file")
    if not name:
        return {"schema_version": 1, "tier2_enabled": False, "actions": []}
    path = site_root / name
    if not path.is_file():
        raise SiteError(f"incident.autonomy_file {name} does not exist")
    allow = load_yaml(path)
    problems = schema_errors(allow, "autonomy.schema.json")
    if problems:
        raise SiteError(f"{name} is not valid: {problems[0]}")
    return allow


def tier1(reason: str) -> dict[str, Any]:
    return {"tier": 1, "reason": reason}


def token_regex(template_token: str) -> str:
    """Regular expression for one template token: literals escaped, each placeholder a named group."""
    seen: set[str] = set()
    out = []
    for index, part in enumerate(PLACEHOLDER.split(template_token)):
        if index % 2 == 0:
            out.append(re.escape(part))
        elif part in seen:
            out.append(f"(?P={part})")
        else:
            seen.add(part)
            out.append(f"(?P<{part}>.+)")
    return "".join(out)


def bind(template: str, tokens: list[str]) -> dict[str, str] | None:
    """Match command tokens against a template, token for token.

    Returns the placeholder values, or None. The token counts must be equal:
    an extra argument is never ignored.
    """
    wanted = shlex.split(template)
    if len(wanted) != len(tokens):
        return None
    values: dict[str, str] = {}
    for template_token, token in zip(wanted, tokens, strict=True):
        found = re.fullmatch(token_regex(template_token), token, re.DOTALL)
        if not found:
            return None
        for name, value in found.groupdict().items():
            if values.setdefault(name, value) != value:
                return None
    return values


def parameters_ok(action: dict[str, Any], values: dict[str, str]) -> str | None:
    """None if every placeholder value satisfies its rule, else why not."""
    rules = action.get("parameters", {})
    for name, value in values.items():
        rule = rules.get(name)
        if rule is None:
            return f"action '{action['id']}' has no rule for {{{name}}}"
        if "values" in rule and value not in rule["values"]:
            return f"{{{name}}}='{value}' is not one of the allowed values for '{action['id']}'"
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            return f"{{{name}}}='{value}' does not match the allowed pattern for '{action['id']}'"
    return None


def claims_so_far(incident_id: str | None, action_id: str) -> int:
    """How many times this action has already been claimed in this incident."""
    if not incident_id:
        return 0
    path = audit_hook.spool_path(incident_id)
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == "tier2" and record.get("action") == action_id:
            count += 1
    return count


def decide(allow: dict[str, Any], command: str, service: str | None, incident_id: str | None) -> dict[str, Any]:
    """The tier of one command, and why."""
    if not allow.get("tier2_enabled"):
        return tier1("Tier 2 is switched off for this site")
    if SHELL_OPERATORS.search(command):
        return tier1(
            "the command uses shell operators, substitution or more than one line; "
            "only a single simple command can be Tier 2"
        )
    try:
        tokens = shlex.split(command)
    except ValueError:
        return tier1("the command could not be parsed")
    if not tokens:
        return tier1("empty command")

    near: str | None = None
    for action in allow.get("actions", []):
        values = bind(action["command"], tokens)
        if values is None:
            continue
        problem = parameters_ok(action, values)
        if problem:
            near = near or problem
            continue
        if not action.get("enabled", True):
            near = near or f"action '{action['id']}' is disabled"
            continue
        if action["service"] in allow.get("disabled_services", []):
            near = near or f"Tier 2 is suspended for {action['service']}"
            continue
        if service and service != action["service"]:
            near = near or f"action '{action['id']}' is for {action['service']}, not {service}"
            continue
        used = claims_so_far(incident_id, action["id"])
        if used >= action["max_per_incident"]:
            near = (
                near
                or f"action '{action['id']}' has reached its limit of {action['max_per_incident']} for this incident"
            )
            continue
        return {
            "tier": 2,
            "action": action["id"],
            "service": action["service"],
            "precondition": action["precondition"],
            "runbook": action.get("runbook"),
            "step": action.get("step"),
            "used": used,
            "max_per_incident": action["max_per_incident"],
        }
    return tier1(near or "no pre-approved action matches this command exactly")


def cmd_match(args: argparse.Namespace, allow: dict[str, Any], incident_id: str | None) -> int:
    print(json.dumps(decide(allow, args.command_text, args.service, incident_id), indent=2))
    return 0


def cmd_claim(args: argparse.Namespace, allow: dict[str, Any], incident_id: str | None) -> int:
    if not incident_id:
        raise SiteError("no current incident; a Tier 2 claim is only meaningful during one")
    decision = decide(allow, args.command_text, args.service, incident_id)
    if decision["tier"] == 2:
        if not args.precondition_evidence:
            decision = tier1(
                f"action '{decision['action']}' matches, but no --precondition-evidence was given. "
                f"Its precondition: {decision['precondition']}"
            )
        else:
            audit_hook.append(
                incident_id,
                {
                    "v": 1,
                    "ts": sre_common.iso_utc(sre_common.utc_now()),
                    "incident": incident_id,
                    "capture": "self-reported",
                    "event": "tier2",
                    "action": decision["action"],
                    "service": decision["service"],
                    "command": redact.redact_text(args.command_text, redact.env_secrets()),
                    "precondition_evidence": redact.redact_text(args.precondition_evidence, redact.env_secrets()),
                },
            )
            decision["used"] += 1
    print(json.dumps(decision, indent=2))
    return 0


def cmd_list(_args: argparse.Namespace, allow: dict[str, Any], _incident_id: str | None) -> int:
    print(json.dumps(allow, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--incident", help="<repo-alias>-<number>; default is the current incident")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, run, text in (
        ("match", cmd_match, "say which tier a command is"),
        ("claim", cmd_claim, "match and record"),
    ):
        one = sub.add_parser(name, help=text)
        one.add_argument("--command", dest="command_text", required=True, help="the exact command, as it would be run")
        one.add_argument("--service", help="the service being acted on, as component:<name>")
        one.set_defaults(run=run)
        if name == "claim":
            one.add_argument(
                "--precondition-evidence", help="what you observed that satisfies the action's precondition"
            )
    sub.add_parser("list", help="show the allow-list").set_defaults(run=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)

    def action() -> int:
        site_root, doc = load_site(args.site)
        allow = load_allow_list(site_root, doc)
        incident_id = args.incident
        if incident_id is None:
            try:
                incident_id = sre_common.read_incident_state()["id"]
            except SiteError:
                incident_id = None
        return args.run(args, allow, incident_id)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
