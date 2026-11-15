#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Run a read-only kubectl command, checked argument by argument.

For an agent driven from chat, this is the only kubectl that runs without
an approval request. A permission rule cannot tell `kubectl get pods -A`
from `kubectl get pods -As https://elsewhere` (bundled short flags), or a
secret named in a URL path from a pod; a parser that knows each option can.
So this script allows a fixed set of read verbs and a fixed set of options,
each in one plain form, refuses everything else, and runs kubectl as an
argument list with no shell.

Refused: any option not listed (so every connection, credential and
impersonation option: --server, -s, --token, --kubeconfig, --as, --user,
--cluster, client certificates), bundled or attached short flags, --raw,
watching or following (a turn would never end), any output format not named
(several read a file), and any argument that names secrets.

Usage: kube_read.py <verb> [arguments]   e.g. kube_read.py get pods -n web -o wide
Exit codes: kubectl's own, or 2 if the command was refused.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - argument list, no shell
import sys

VERBS = {
    ("get",),
    ("describe",),
    ("logs",),
    ("top", "pod"),
    ("top", "pods"),
    ("top", "node"),
    ("top", "nodes"),
    ("events",),
    ("api-resources",),
    ("version",),
    ("config", "current-context"),
    ("config", "get-contexts"),
}
# Options that take a value: long name -> short form, if it has one.
VALUE_OPTIONS = {
    "--namespace": "-n",
    "--output": "-o",
    "--selector": "-l",
    "--label-columns": "-L",
    "--container": "-c",
    "--field-selector": None,
    "--context": None,
    "--since": None,
    "--since-time": None,
    "--tail": None,
    "--sort-by": None,
    "--for": None,
    "--types": None,
    "--api-group": None,
    "--chunk-size": None,
    "--limit-bytes": None,
    "--max-log-requests": None,
    "--request-timeout": None,
}
FLAG_OPTIONS = {
    "--all-namespaces": "-A",
    "--previous": "-p",
    "--show-labels": None,
    "--no-headers": None,
    "--all-containers": None,
    "--timestamps": None,
    "--prefix": None,
    "--containers": None,
    "--ignore-not-found": None,
    "--show-managed-fields": None,
    "--show-kind": None,
    "--namespaced": None,
    "--client": None,
}
SHORT = {short: long for long, short in {**VALUE_OPTIONS, **FLAG_OPTIONS}.items() if short}
# Positional arguments and option values: names, selectors, durations, simple jsonpath.
# No quotes, no shell characters; a jsonpath filter expression is not supported.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._/:,=@+{}\[\]*-]{1,400}$")
SECRET = re.compile(r"secret", re.IGNORECASE)
# Output formats are allowed by name, not refused by pattern: kubectl reads a file
# for several spellings (go-template-file, templatefile, jsonpath-file and more).
OUTPUT = re.compile(r"^(?:wide|name|json|yaml|(?:jsonpath|jsonpath-as-json|custom-columns|go-template)=.+)$")
TIMEOUT_SECONDS = 120


class Refused(Exception):
    """The command is not one this script runs."""


def _option(token: str) -> tuple[str, str | None]:
    """(long option name, attached value) for an option token. Raises Refused for anything not listed."""
    if token.startswith("--"):
        name, eq, value = token.partition("=")
        if name not in VALUE_OPTIONS and name not in FLAG_OPTIONS:
            raise Refused(f"option {name} is not allowed")
        if eq and name in FLAG_OPTIONS:
            raise Refused(f"option {name} takes no value")
        return name, value if eq else None
    if token in SHORT:
        return SHORT[token], None
    raise Refused(f"option {token} is not allowed; write short options alone (-n web), never bundled or attached")


def _check_value(name: str, value: str) -> None:
    if not SAFE_VALUE.fullmatch(value):
        raise Refused(f"the value of {name} has characters this script does not pass on")
    if SECRET.search(value):
        raise Refused("secrets are never read from chat")
    if name == "--output" and not OUTPUT.fullmatch(value):
        raise Refused(
            "output format must be wide, name, json, yaml, "
            "jsonpath=, jsonpath-as-json=, custom-columns= or go-template="
        )


def check(argv: list[str]) -> list[str]:
    """The kubectl argument list for an allowed command. Raises Refused otherwise."""
    verb = next((v for v in sorted(VERBS, key=len, reverse=True) if tuple(argv[: len(v)]) == v), None)
    if verb is None:
        raise Refused(f"verb {' '.join(argv[:2]) or '(none)'} is not one of the read verbs")
    rest = argv[len(verb) :]
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            raise Refused("'--' is not allowed")
        if word.startswith("-"):
            name, value = _option(word)
            if name in VALUE_OPTIONS and value is None:
                index += 1
                if index >= len(rest):
                    raise Refused(f"option {name} needs a value")
                value = rest[index]
            if value is not None:
                _check_value(name, value)
        else:
            _check_value("an argument", word)
        index += 1
    return list(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    try:
        args = check(argv)
    except Refused as exc:
        print(f"REFUSED {exc}. Ask in the channel for an approval to run it directly.", file=sys.stderr)
        return 2
    kubectl = shutil.which("kubectl")
    if not kubectl:
        print("ERROR kubectl is not installed", file=sys.stderr)
        return 2
    try:
        done = subprocess.run([kubectl, *args], check=False, timeout=TIMEOUT_SECONDS)  # noqa: S603 - checked argument list
    except subprocess.TimeoutExpired:
        print(f"ERROR kubectl did not finish in {TIMEOUT_SECONDS}s", file=sys.stderr)
        return 2
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
