#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scan a directory for site identifiers that must not ship in the portable plugin.

Two layers:

* Generic patterns (always on): private IP ranges, home-directory paths,
  internal-looking hostnames, chat IDs and secret-shaped strings. Safe to run
  in public CI because the patterns name no real system.
* A denylist (--denylist FILE): the real hostnames, domains, addresses and
  usernames of one site. The file lives in the private site repository and is
  only ever used locally.

A line containing the marker ``identifier-allow`` is skipped, for deliberate
examples. Exit codes: 0 clean, 1 findings.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ALLOW_MARKER = "identifier-allow"
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules", ".venv"}

GENERIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private IP address",
        re.compile(r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"),
    ),
    ("home directory path", re.compile(r"(?:/home|/Users)/[A-Za-z_][A-Za-z0-9_.-]*/")),
    ("internal hostname", re.compile(r"\b[a-z0-9-]+\.int\.[a-z0-9-]+(?:\.[a-z0-9-]+)*\b")),
    ("internal hostname", re.compile(r"\b[a-z0-9-]+\.(?:lan|local|internal)\b(?!\.?[a-z0-9])")),
    ("chat ID", re.compile(r"\b[UCWT](?=[A-Z0-9]*\d)[A-Z0-9]{8,11}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("token", re.compile(r"\b(?:gh[opsu]|github_pat|glc|glsa)_[A-Za-z0-9+/=_-]{20,}")),
    ("token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("credentials in URL", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
)


def load_denylist(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    """Read literal identifiers, one per line. '#' starts a comment."""
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            patterns.append(
                ("denylisted identifier", re.compile(rf"(?<![\w-]){re.escape(entry)}(?![\w-])", re.IGNORECASE))
            )
    return patterns


def iter_files(root: Path):
    """Yield text files under root, skipping tool caches."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def scan(root: Path, patterns: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Return one finding per offending match."""
    findings = []
    for path in iter_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if ALLOW_MARKER in line:
                continue
            for label, pattern in patterns:
                match = pattern.search(line)
                if match:
                    findings.append(f"{path.relative_to(root)}:{number}: {label}: {match.group(0)}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="directory to scan")
    parser.add_argument("--denylist", type=Path, help="file of real site identifiers (kept private)")
    parser.add_argument("--denylist-only", action="store_true", help="skip the generic patterns")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        parser.error(f"not a directory: {args.root}")
    if args.denylist_only and not args.denylist:
        parser.error("--denylist-only needs --denylist")

    patterns = [] if args.denylist_only else list(GENERIC_PATTERNS)
    if args.denylist:
        patterns.extend(load_denylist(args.denylist))

    findings = scan(args.root, patterns)
    for finding in findings:
        print(finding)
    print("clean" if not findings else f"{len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
