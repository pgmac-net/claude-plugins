#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Alerts that qualify for unattended triage.

The chat gateway polls the site's alert source and starts a triage-only agent
session for each new batch of qualifying problems. This module holds the
rules, so they can be run and tested without the gateway:

- A problem qualifies when it is HARD, unhandled (not acknowledged, not in
  scheduled downtime, notifications on), has lasted at least
  runner.min_state_age_seconds, and is on a host that a catalog service in one
  of runner.tiers claims directly (dependsOn a Resource monitoring that host).
- A problem is new once: its key is host, check and the time its state last
  changed. A flap is a new state change, so it is a new problem; the per-service
  cooldown is what stops a flapping check from starting triage after triage.

Commands:
  check   list the problems that would start a triage now (read-only)

Exit codes: 0 ok, 2 the site or the alert source could not be used.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sre_common
from sre_common import SiteError, guarded, load_site

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "tiers": [1],
    "poll_seconds": 60,
    "max_auto_sessions": 1,
    "cooldown_minutes": 30,
    "min_state_age_seconds": 120,
}

# statusjson.cgi status values (bit flags).
HOST_PROBLEMS = {4: "DOWN", 8: "UNREACHABLE"}
SERVICE_PROBLEMS = {4: "WARNING", 8: "UNKNOWN", 16: "CRITICAL"}
HARD = 1
OUTPUT_LIMIT = 300
STATUS_TIMEOUT = 20


@dataclass(frozen=True)
class Alert:
    """One problem reported by the alert source. 'check' is None for a host problem."""

    host: str
    check: str | None
    state: str
    output: str
    since: int  # epoch seconds of the last state change
    services: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        return f"{self.host}/{self.check}" if self.check else self.host

    @property
    def key(self) -> str:
        return f"{self.target}@{self.since}"

    def line(self) -> str:
        """One line for a person or the agent. The output is the plugin's text: untrusted, so it is cut short."""
        output = " ".join(self.output.split())[:OUTPUT_LIMIT]
        scope = f" [{', '.join(self.services)}]" if self.services else ""
        return f"{self.target} is {self.state}{scope}: {output}"


@dataclass
class Selection:
    """What one poll found: problems that start a triage, and why the others did not."""

    start: list[Alert] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def settings(doc: dict[str, Any]) -> dict[str, Any]:
    """The site's runner settings, with defaults filled in."""
    return {**DEFAULTS, **(doc.get("runner") or {})}


def source(doc: dict[str, Any]) -> dict[str, Any]:
    """The monitoring entry the runner polls."""
    name = settings(doc).get("source")
    for entry in doc.get("monitoring", []):
        if entry["name"] == name:
            if entry["type"] != "nagios":
                raise SiteError(f"runner.source '{name}' is a {entry['type']} source; only nagios is supported")
            return entry
    raise SiteError(f"runner.source '{name}' names no monitoring entry")


def status_url(doc: dict[str, Any]) -> str:
    """Where statusjson.cgi is: runner.status_url, or under the source's endpoint."""
    explicit = settings(doc).get("status_url")
    if explicit:
        return explicit
    endpoint = source(doc).get("endpoint")
    if not endpoint:
        raise SiteError("runner needs status_url, or an endpoint on its monitoring source")
    return endpoint.rstrip("/") + "/cgi-bin/statusjson.cgi"


def credentials(doc: dict[str, Any], env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(user, password) for the alert source from the environment, or None if the site names none."""
    env = dict(os.environ) if env is None else env
    runner = settings(doc)
    user_env, password_env = runner.get("user_env"), runner.get("password_env")
    if not user_env and not password_env:
        return None
    user, password = env.get(user_env or "", ""), env.get(password_env or "", "")
    if not user or not password:
        raise SiteError(f"environment variables {user_env} and {password_env} must both be set for the runner")
    return user, password


def fetch(url: str, query: str, auth: tuple[str, str] | None, timeout: float = STATUS_TIMEOUT) -> dict[str, Any]:
    """One statusjson.cgi query, returning its 'data'."""
    status = "down+unreachable" if query == "hostlist" else "warning+critical+unknown"
    key = "hoststatus" if query == "hostlist" else "servicestatus"
    params = urllib.parse.urlencode({"query": query, "details": "true", key: status}, safe="+")
    if not url.startswith(("https://", "http://")):
        raise SiteError("the runner's status URL must be http or https")
    request = urllib.request.Request(f"{url}?{params}", headers={"Accept": "application/json"})  # noqa: S310
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        # The URL can carry no secret (auth is a header), but keep the message to the type.
        raise SiteError(f"the alert source could not be read: {type(exc).__name__}") from exc
    result = body.get("result") or {}
    if result.get("type_code", 0) != 0:
        raise SiteError(f"the alert source refused the query: {result.get('type_text', 'error')}")
    return body.get("data") or {}


def _seconds(value: Any) -> int:
    """statusjson times are milliseconds since the epoch; accept seconds too."""
    number = int(value or 0)
    return number // 1000 if number > 10**11 else number


def _unhandled(item: dict[str, Any]) -> bool:
    return (
        item.get("state_type") == HARD
        and not item.get("problem_has_been_acknowledged")
        and int(item.get("scheduled_downtime_depth") or 0) == 0
        and item.get("notifications_enabled", True) is not False
    )


def parse(hosts: dict[str, Any], services: dict[str, Any]) -> list[Alert]:
    """Unhandled HARD problems from statusjson hostlist and servicelist data."""
    found = []
    for name, item in (hosts.get("hostlist") or {}).items():
        state = HOST_PROBLEMS.get(item.get("status"))
        if state and _unhandled(item):
            found.append(
                Alert(name, None, state, str(item.get("plugin_output", "")), _seconds(item.get("last_state_change")))
            )
    down = {alert.host for alert in found}
    for host, checks in (services.get("servicelist") or {}).items():
        if host in down:
            # The host problem is the alert; its checks failing with it add nothing.
            continue
        for check, item in (checks or {}).items():
            state = SERVICE_PROBLEMS.get(item.get("status"))
            if state and _unhandled(item):
                output = str(item.get("plugin_output", ""))
                found.append(Alert(host, check, state, output, _seconds(item.get("last_state_change"))))
    return sorted(found, key=lambda alert: (alert.since, alert.target))


def host_services(site_root: Path, doc: dict[str, Any]) -> dict[str, list[tuple[str, int | None]]]:
    """Monitored host name -> [(service ref, tier)] for the services that claim it directly."""
    import catalog_check  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    report = catalog_check.CatalogReport()
    entities = catalog_check.load_entities(site_root / doc["catalog"]["path"], report)
    resources: dict[str, str] = {}
    for entity in entities:
        host = ((entity.spec.get("sre") or {}).get("monitoring") or {}).get("host")
        if entity.doc["kind"] == "Resource" and host:
            resources[entity.ref] = host
    found: dict[str, list[tuple[str, int | None]]] = {}
    for entity in entities:
        if entity.doc["kind"] != "Component":
            continue
        tier = (entity.spec.get("sre") or {}).get("tier")
        for ref in entity.spec.get("dependsOn") or []:
            if ref in resources:
                found.setdefault(resources[ref], []).append((entity.ref, tier))
    return found


def select(  # noqa: PLR0913  pylint: disable=too-many-arguments
    alerts: list[Alert],
    index: dict[str, list[tuple[str, int | None]]],
    runner: dict[str, Any],
    *,
    seen: set[str],
    cooldowns: dict[str, float],
    now: float,
) -> Selection:
    """Which problems start a triage now. 'cooldowns' maps a service ref to when its last triage started."""
    result = Selection()
    tiers = set(runner["tiers"])
    for alert in alerts:
        if alert.key in seen:
            continue
        services = tuple(sorted({ref for ref, tier in index.get(alert.host, []) if tier in tiers}))
        if not services:
            result.skipped[alert.key] = "no service in a triaged tier claims this host"
            continue
        if now - alert.since < runner["min_state_age_seconds"]:
            result.skipped[alert.key] = "too recent; checked again next poll"
            continue
        cooling = [ref for ref in services if now - cooldowns.get(ref, 0) < runner["cooldown_minutes"] * 60]
        if cooling and len(cooling) == len(services):
            result.skipped[alert.key] = f"cooling down after a recent triage of {', '.join(cooling)}"
            continue
        result.start.append(Alert(alert.host, alert.check, alert.state, alert.output, alert.since, services))
    return result


def cmd_check(args: argparse.Namespace, site_root: Path, doc: dict[str, Any]) -> int:
    runner = settings(doc)
    url = status_url(doc)
    auth = credentials(doc)
    alerts = parse(fetch(url, "hostlist", auth), fetch(url, "servicelist", auth))
    index = host_services(site_root, doc)
    now = sre_common.utc_now().timestamp()
    chosen = select(alerts, index, runner, seen=set(), cooldowns={}, now=now)
    print(
        json.dumps(
            {
                "enabled": runner["enabled"],
                "problems": len(alerts),
                "would_start": [{**asdict(alert), "key": alert.key} for alert in chosen.start],
                "skipped": chosen.skipped if args.verbose else len(chosen.skipped),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="list the problems that would start a triage now")
    check.add_argument("--verbose", action="store_true", help="say why each other problem was skipped")
    check.set_defaults(run=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    args = build_parser().parse_args(argv)

    def action() -> int:
        site_root, doc = load_site(args.site)
        if not doc.get("runner"):
            raise SiteError("site.yaml has no runner section")
        return args.run(args, site_root, doc)

    return guarded(action)


if __name__ == "__main__":
    sys.exit(main())
