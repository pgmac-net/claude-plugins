#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6", "typesafe-sdk>=0.7.5"]
# ///
"""Advisory second opinions during an incident, from a fast judgment model.

Three questions, each answered with a choice and its probabilities:

  severity    which severity level an impact description suggests
  runbooks    which runbooks best match a description of the symptoms
  entry-type  which timeline entry type a piece of text is

Everything here is advice. It never approves, blocks or selects an action,
never decides whether something is Tier 2, and never sets a final severity.
The agent and the person decide; this is one more input.

Using it sends text to a third party, so it is off unless the site turns it
on. Before anything is sent, secrets are redacted and site identifiers
(from the site's mask file) and private addresses are replaced with
placeholders.

It never fails an incident: when it is off, has no key, times out or errors,
it prints {"available": false, "reason": …} and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import audit_hook
import redact
import sre_common
from sre_common import SEVERITIES, SiteError, load_site

DEFAULT_TIMEOUT = 8
MAX_FIELD = 6000
if redact.MAX_SCAN < MAX_FIELD:  # every character that can be sent must be scanned for secrets
    raise RuntimeError("MAX_FIELD must not exceed redact.MAX_SCAN")
NONE_OPTION = "none"
WHITESPACE = re.compile(r"\s")
OMITTED = "[omitted: too long to send safely]"
# Addresses that only mean something inside a site: RFC 1918, CGNAT, link-local,
# IPv6 unique-local and link-local.
PRIVATE_ADDRESS = re.compile(
    r"(?<![\d.])(?:"
    r"10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168|169\.254"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])"
    r")\.\d{1,3}\.\d{1,3}(?![\d.])"
    r"|(?<![0-9A-Fa-f:])(?:[fF][cCdD][0-9A-Fa-f]{2}|[fF][eE][89aAbB][0-9A-Fa-f])"
    r"(?::[0-9A-Fa-f]{0,4}){1,7}(?![0-9A-Fa-f:])"
)
GENERIC_SEVERITY = {
    "P1": "Critical. A service many people or other services depend on is down or losing data, with no workaround.",
    "P2": "Major. An important service is down or badly degraded, or a critical one is at risk.",
    "P3": "Minor. A limited part of a service is degraded, or a non-critical service is down; impact is contained.",
    "P4": "Low. Cosmetic, a single user, or a fault with no current impact.",
}
ENTRY_TYPES = {
    "discovery": "Something learned: a symptom confirmed, what a monitoring source or diagnostic showed.",
    "action": "A change made to a system, and what happened afterwards.",
    "decision": "A choice between options: a runbook matched or not, a severity changed, a shortcut taken.",
    "milestone": "A state reached or missed: mitigated, recovered, a deadline passed.",
    "update": "A status update: the state now, what changed since the last update, what is next.",
    "handover": "Passing the incident to someone else: state, what was tried, hypotheses, next step.",
    "summary": "A digest of the whole incident: what happened, what fixed it, what is still open.",
}


class Unavailable(Exception):
    """The helper cannot answer; the caller carries on without it."""


def settings(doc: dict[str, Any]) -> dict[str, Any]:
    """The site's assist settings, or why the helper is unavailable."""
    assist = doc["incident"].get("assist")
    if not assist or not assist.get("enabled"):
        raise Unavailable("the advisory helper is not enabled for this site")
    key_env = assist.get("api_key_env")
    if not key_env or not os.environ.get(key_env):
        raise Unavailable(f"no API key in the environment variable {key_env or '(none named)'}")
    return assist


class Masker:
    """Replaces site identifiers and private addresses with stable placeholders.

    The same identifier always gets the same placeholder within one call, so
    the text still makes sense to a reader who never learns the real names.
    """

    def __init__(self, identifiers: list[str], env_values: dict[str, str]):
        self.identifiers = sorted({item for item in identifiers if item}, key=len, reverse=True)
        self.env_values = env_values
        self.names: dict[str, str] = {}

    @classmethod
    def for_site(cls, site_root: Path, assist: dict[str, Any], extra_env: list[str]) -> Masker:
        identifiers: list[str] = []
        mask_file = assist.get("mask_file")
        if mask_file:
            path = site_root / mask_file
            if not path.is_file():
                raise Unavailable(f"the mask file {mask_file} is missing; not sending unmasked text")
            for raw in path.read_text(encoding="utf-8").splitlines():
                entry = raw.split("#", 1)[0].strip()
                if entry:
                    identifiers.append(entry)
        return cls(identifiers, redact.env_secrets(extra_names=extra_env))

    def _placeholder(self, kind: str, value: str) -> str:
        key = value.lower()
        if key not in self.names:
            self.names[key] = f"{kind}-{sum(1 for name in self.names.values() if name.startswith(kind)) + 1}"
        return self.names[key]

    def text(self, value: str) -> str:
        """Cut, then redact secrets, then mask identifiers and private addresses.

        The order is what makes it safe:

        1. Cut first, to a window small enough that every character of it is
           scanned. Nothing past the cut can be sent, masked or not. The cut is
           always at whitespace, so no token is ever split and half a secret or
           half an identifier cannot be left at the end. Text with no
           whitespace to cut at is not sent at all.
        2. Redact secrets while the text is still as written. Masking first
           could change part of a secret and stop its pattern matching.
        3. Mask identifiers and addresses last, wherever they are not part of
           a longer run of letters and digits.

        Nothing derived from the uncut text, such as a hash of it, is added.
        """
        if len(value) > MAX_FIELD:
            breaks = [match.start() for match in WHITESPACE.finditer(value, 0, MAX_FIELD + 1)]
            if not breaks:
                # One unbroken run longer than the limit. Any cut would split it, so none of it is sent.
                return OMITTED
            value = value[: breaks[-1]]
        value = redact.redact_text(value, self.env_values, limit=None, digest=False)
        return self._mask_names(value)

    def _mask_names(self, value: str) -> str:
        """Replace identifiers and private addresses in one pass, so neither can break the other.

        Every identifier and every private address is found in the original text, then all of them
        are replaced at once. Where matches overlap they are merged into a single region, so a full
        address under a listed prefix, an identifier that contains an address, and two identifiers
        that overlap are each masked whole, with nothing left over. Placeholders are numbered in
        order of appearance.
        """
        found: list[tuple[int, int, str]] = [
            (match.start(), match.end(), "address") for match in PRIVATE_ADDRESS.finditer(value)
        ]
        for identifier in self.identifiers:
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])", re.IGNORECASE)
            found.extend((match.start(), match.end(), "site") for match in pattern.finditer(value))
        # Overlapping matches become one region covering all of them, so no character of any of
        # them survives. Matches that merely touch stay separate. The region is labelled by its
        # longest member: a full address under a listed prefix is an address, an identifier that
        # contains an address is a site name.
        regions: list[list[Any]] = []  # start, end, kind, length of the longest member
        for start, end, kind in sorted(found):
            if regions and start < regions[-1][1]:
                region = regions[-1]
                region[1] = max(region[1], end)
                if end - start > region[3]:
                    region[2], region[3] = kind, end - start
            else:
                regions.append([start, end, kind, end - start])
        chosen = [(start, end, kind) for start, end, kind, _length in regions]
        out, position = [], 0
        for start, end, kind in chosen:
            out.append(value[position:start])
            out.append(self._placeholder(kind, value[start:end]))
            position = end
        out.append(value[position:])
        return "".join(out)

    def node(self, node: Any) -> Any:
        """Mask every string in a JSON-like structure, keys included."""
        if isinstance(node, dict):
            return {self.text(str(key)): self.node(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.node(value) for value in node]
        return self.text(node) if isinstance(node, str) else node


def ask(assist: dict[str, Any], state: dict[str, Any], instructions: str, options: dict[str, str]) -> dict[str, Any]:
    """Send one Choice question. Returns {'choice', 'confidence', 'probabilities', 'model'}.

    The only function that talks to the service; tests replace it.
    """
    # Imported here so that everything else in this file works without the SDK installed.
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient, TypeSafeError  # noqa: PLC0415  pylint: disable=import-outside-toplevel,import-error

    timeout = float(assist.get("timeout_seconds", DEFAULT_TIMEOUT))
    try:
        client = TypeSafeClient(
            api_key=os.environ[assist["api_key_env"]],
            model=assist.get("model"),
            timeout=timeout,
            retry=RetryPolicy(max_retries=1, timeout=timeout),
        )
        with client:
            result = client.system_one(state, {"q": Choice(instructions=instructions, criteria=options)})
    except TypeSafeError as exc:
        raise Unavailable(f"the helper's service did not answer: {type(exc).__name__}") from exc
    answer = result.choices["q"]
    return checked(
        {
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": dict(answer.probabilities),
            "model": result.model,
        },
        options,
    )


def checked(answer: dict[str, Any], options: dict[str, str]) -> dict[str, Any]:
    """Accept an answer only if it is made of the options that were offered.

    Nothing the service returns is passed on as free text: the choice must be
    one of our own labels, and probabilities are kept only for those labels.
    """
    if answer.get("choice") not in options:
        raise Unavailable("the helper returned an answer that was not one of the options")
    probabilities = {}
    for label in options:
        value = answer.get("probabilities", {}).get(label, 0.0)
        probabilities[label] = round(float(value), 3) if isinstance(value, int | float) else 0.0
    confidence = answer.get("confidence")
    model = str(answer.get("model", ""))
    return {
        "choice": answer["choice"],
        "confidence": round(float(confidence), 3) if isinstance(confidence, int | float) else None,
        "probabilities": probabilities,
        "model": model if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", model) else "unknown",
    }


def ranked(probabilities: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))


def suggest_severity(
    _site_root: Path, doc: dict[str, Any], assist: dict[str, Any], masker: Masker, args
) -> dict[str, Any]:
    state: dict[str, Any] = {"impact": args.impact}
    if args.affected:
        state["affected"] = args.affected
    options = dict(GENERIC_SEVERITY)
    criteria = doc["incident"].get("severity_criteria")
    repo = doc["repos"].get(criteria["repo"], {}) if criteria else {}
    if criteria and repo.get("path"):
        path = Path(repo["path"]).expanduser() / criteria["path"]
        if path.is_file():
            state["site_criteria"] = path.read_text(encoding="utf-8")
            options = {level: f"{level} as defined in `site_criteria`." for level in SEVERITIES}
    answer = ask(
        assist,
        masker.node(state),
        "Which severity level best fits the incident described in `impact`?",
        options,
    )
    return {
        "kind": "severity",
        "suggestion": answer["choice"],
        "confidence": answer["confidence"],
        "probabilities": dict(ranked(answer["probabilities"])),
        "used_site_criteria": "site_criteria" in state,
        "model": answer["model"],
        "note": "A suggestion. Severity is provisional and is yours and the person's to set.",
    }


def runbook_candidates(doc: dict[str, Any]) -> dict[str, str]:
    """Runbook file name → its title and first lines, read from the local checkout."""
    alias = doc["catalog"].get("runbooks_repo", "")
    repo = doc["repos"].get(alias, {})
    if not repo.get("path") or not repo.get("runbooks_path"):
        raise Unavailable("the site names no local runbooks directory")
    directory = Path(repo["path"]).expanduser() / repo["runbooks_path"]
    if not directory.is_dir():
        raise Unavailable(f"runbooks directory not found: {directory}")
    found = {}
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() in ("index.md", "readme.md"):
            continue
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        body = [line for line in lines if line and not line.startswith(("---", "tags:", "title:", "- ", "#", ">", "|"))]
        title = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), path.stem)
        found[path.name] = f"{title}. {' '.join(body)[:300]}"
    if not found:
        raise Unavailable("no runbooks found")
    return found


def suggest_runbooks(
    _site_root: Path, doc: dict[str, Any], assist: dict[str, Any], masker: Masker, args
) -> dict[str, Any]:
    candidates = runbook_candidates(doc)
    # Options are numbered so that a file name never has to survive masking.
    labels = {f"r{index}": name for index, name in enumerate(candidates, start=1)}
    options = {label: masker.text(candidates[name]) for label, name in labels.items()}
    options[NONE_OPTION] = "None of the runbooks covers the situation in `symptoms`."
    answer = ask(
        assist,
        masker.node({"symptoms": args.symptoms}),
        "Which runbook best covers the situation described in `symptoms`?",
        options,
    )
    order = ranked(answer["probabilities"])
    top = [
        {"runbook": labels[label], "probability": value} for label, value in order if label in labels and value >= 0.01
    ][: args.top]
    return {
        "kind": "runbooks",
        "ranked": top,
        "none_probability": answer["probabilities"].get(NONE_OPTION, 0.0),
        "candidates": len(candidates),
        "confidence": answer["confidence"],
        "model": answer["model"],
        "note": "A ranking to read from, not a match. Read the top runbooks before acting on one.",
    }


def suggest_entry_type(
    _site_root: Path, _doc: dict[str, Any], assist: dict[str, Any], masker: Masker, args
) -> dict[str, Any]:
    answer = ask(
        assist,
        masker.node({"entry": args.text}),
        "Which kind of incident timeline entry is the text in `entry`?",
        dict(ENTRY_TYPES),
    )
    return {
        "kind": "entry-type",
        "suggestion": answer["choice"],
        "confidence": answer["confidence"],
        "probabilities": dict(ranked(answer["probabilities"])),
        "model": answer["model"],
        "note": "A suggestion. Choose the type yourself when you know it.",
    }


def log_to_audit(result: dict[str, Any]) -> None:
    """Record the advice in the current incident's audit log, if there is one being captured."""
    try:
        incident_id = sre_common.read_incident_state()["id"]
    except SiteError:
        return
    if audit_hook.is_closed(incident_id) or not audit_hook.spool_path(incident_id).is_file():
        return
    keys = ("kind", "suggestion", "ranked", "confidence", "model", "available", "reason")
    audit_hook.self_report(incident_id, "assist", **{key: result[key] for key in keys if key in result})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="site repository path")
    sub = parser.add_subparsers(dest="kind", required=True)

    severity = sub.add_parser("severity", help="suggest a severity level")
    severity.add_argument("--impact", required=True, help="what is broken, for whom")
    severity.add_argument("--affected", help="services or hosts affected")
    severity.set_defaults(run=suggest_severity)

    runbooks = sub.add_parser("runbooks", help="rank runbooks against symptoms")
    runbooks.add_argument("--symptoms", required=True, help="what is being observed")
    runbooks.add_argument("--top", type=int, default=5, help="how many to return")
    runbooks.set_defaults(run=suggest_runbooks)

    entry = sub.add_parser("entry-type", help="suggest a timeline entry type")
    entry.add_argument("--text", required=True, help="the entry text")
    entry.set_defaults(run=suggest_entry_type)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Always exits 0: advice that cannot be had is not an error."""
    args = build_parser().parse_args(argv)
    try:
        site_root, doc = load_site(args.site)
        assist = settings(doc)
        names = [name for source in doc.get("monitoring", []) for name in source.get("credentials_env", [])]
        masker = Masker.for_site(site_root, assist, [*names, assist["api_key_env"]])
        result = {"available": True, "advisory": True, **args.run(site_root, doc, assist, masker, args)}
    except (Unavailable, SiteError) as exc:
        result = {"available": False, "advisory": True, "kind": args.kind, "reason": str(exc)}
    log_to_audit(result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
