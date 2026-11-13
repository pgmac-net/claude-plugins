#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Decide whether a command is a pre-approved (Tier 2) action, and run it.

The decision is made here, by exact matching against the site's allow-list,
not by the agent's judgment. A command is Tier 2 only if it is written in a
conservative character set (letters, digits and . _ : / = @ , + - separated
by single spaces: no quoting, no shell operators, nothing a shell would
expand), is token for token equal to a listed template with every variable
part satisfying that template's rule, is for a service that is not
suspended, and is within the action's limit for this incident. Everything
else is Tier 1: stop and ask.

A Tier 2 action is executed by this script, as an argument list with no
shell. The agent never passes a pre-approved command to a shell itself, so
what was matched is exactly what runs.

Commands:
  match   say which tier a command is; changes nothing
  run     match; if Tier 2 and evidence of the precondition is given, record
          the claim in the audit log, execute the command without a shell,
          and record its exit code
  list    show the allow-list

Exit codes: match and list: 0, read 'tier' in the output. run: the command's
own exit code if it ran, 10 if it was Tier 1 and so did not run. 2 the site
or the allow-list could not be used.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # noqa: S404 - argument list, no shell
import sys
from pathlib import Path
from typing import Any

import audit_hook
import redact
import sre_common
from sre_common import SiteError, guarded, load_site, load_yaml, schema_errors

# Anything outside this set could mean something to a shell that it does not mean to us:
# quoting, expansion, globbing, substitution, comments, history, escapes.
SAFE_COMMAND = re.compile(r"[A-Za-z0-9._:/=@,+-]+(?: [A-Za-z0-9._:/=@,+-]+)*")
SAFE_TEMPLATE = re.compile(r"[A-Za-z0-9._:/=@,+{}-]+(?: [A-Za-z0-9._:/=@,+{}-]+)*")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
NOT_RUN = 10


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
    wanted = template.split(" ")
    if len(wanted) != len(tokens):
        return None
    values: dict[str, str] = {}
    for template_token, token in zip(wanted, tokens, strict=True):
        found = re.fullmatch(token_regex(template_token), token)
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


def _unavailable(allow: dict[str, Any], action: dict[str, Any], service: str | None, used: int) -> str | None:
    """Why a matching action cannot be used right now, or None."""
    if not action.get("enabled", True):
        return f"action '{action['id']}' is disabled"
    if action["service"] in allow.get("disabled_services", []):
        return f"Tier 2 is suspended for {action['service']}"
    if service and service != action["service"]:
        return f"action '{action['id']}' is for {action['service']}, not {service}"
    if used >= action["max_per_incident"]:
        return f"action '{action['id']}' has reached its limit of {action['max_per_incident']} for this incident"
    return None


def decide(allow: dict[str, Any], command: str, service: str | None, incident_id: str | None) -> dict[str, Any]:
    """The tier of one command, and why."""
    if not allow.get("tier2_enabled"):
        return tier1("Tier 2 is switched off for this site")
    if not SAFE_COMMAND.fullmatch(command):
        return tier1(
            "the command contains something outside the allowed character set (letters, digits, . _ : / = @ , + - "
            "and single spaces). Quoting, shell operators and anything a shell would expand are never Tier 2"
        )
    tokens = command.split(" ")
    if tokens[0].startswith("-") or "=" in tokens[0]:
        return tier1("the command does not start with a program name")

    near: str | None = None
    for action in allow.get("actions", []):
        if not SAFE_TEMPLATE.fullmatch(action["command"]):
            continue
        values = bind(action["command"], tokens)
        if values is None:
            continue
        used = claims_so_far(incident_id, action["id"])
        problem = parameters_ok(action, values) or _unavailable(allow, action, service, used)
        if problem:
            near = near or problem
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


def record(incident_id: str, event: str, **fields: Any) -> None:
    """Append one self-reported record about a Tier 2 action."""
    audit_hook.append(
        incident_id,
        {
            "v": 1,
            "ts": sre_common.iso_utc(sre_common.utc_now()),
            "incident": incident_id,
            "capture": "self-reported",
            "event": event,
            **fields,
        },
    )


def cmd_match(args: argparse.Namespace, allow: dict[str, Any], incident_id: str | None) -> int:
    print(json.dumps(decide(allow, args.command_text, args.service, incident_id), indent=2))
    return 0


def cmd_run(args: argparse.Namespace, allow: dict[str, Any], incident_id: str | None) -> int:
    if not incident_id:
        raise SiteError("no current incident; a Tier 2 action is only available during one")
    if sre_common.read_incident_state(incident_id).get("drill"):
        raise SiteError("this incident is a drill; a drill never mutates anything. Use 'match' to exercise the path")
    decision = decide(allow, args.command_text, args.service, incident_id)
    if decision["tier"] == 2 and not args.precondition_evidence:
        decision = tier1(
            f"action '{decision['action']}' matches, but no --precondition-evidence was given. "
            f"Its precondition: {decision['precondition']}"
        )
    if decision["tier"] != 2:
        print(json.dumps(decision, indent=2))
        return NOT_RUN

    argv = args.command_text.split(" ")
    program = shutil.which(argv[0])
    if not program:
        raise SiteError(f"'{argv[0]}' is not on PATH; nothing was run")
    evidence = redact.redact_text(args.precondition_evidence, redact.env_secrets())
    record(
        incident_id,
        "tier2",
        action=decision["action"],
        service=decision["service"],
        command=args.command_text,
        precondition_evidence=evidence,
    )
    try:
        done = subprocess.run(  # noqa: S603 - argument list, no shell, matched against the allow-list
            [program, *argv[1:]], capture_output=True, text=True, check=False, timeout=args.timeout
        )
        exit_code, stdout, stderr = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = 124, "", f"timed out after {args.timeout}s\n"
    record(incident_id, "tier2_result", action=decision["action"], command=args.command_text, exit_code=exit_code)
    decision.update({"used": decision["used"] + 1, "ran": True, "exit_code": exit_code})
    print(json.dumps(decision, indent=2))
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code


def cmd_list(_args: argparse.Namespace, allow: dict[str, Any], _incident_id: str | None) -> int:
    print(json.dumps(allow, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    parser.add_argument("--incident", help="<repo-alias>-<number>; default is the current incident")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, run, text in (("match", cmd_match, "say which tier a command is"), ("run", cmd_run, "match and execute")):
        one = sub.add_parser(name, help=text)
        one.add_argument("--command", dest="command_text", required=True, help="the exact command")
        one.add_argument("--service", help="the service being acted on, as component:<name>")
        one.set_defaults(run=run)
        if name == "run":
            one.add_argument("--precondition-evidence", help="what you observed that satisfies the precondition")
            one.add_argument("--timeout", type=int, default=120, help="seconds before the command is abandoned")
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
