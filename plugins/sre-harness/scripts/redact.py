"""Secret detection and redaction shared by the validator and the audit log.

Standard library only: the audit hook imports this on every tool call during
an incident and must start fast and never fail for want of a package.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from typing import Any

# Every pattern is linear: bounded repeats, no nested or lazy quantifiers over long spans.
# A pattern with a group redacts only the group, so the shape of the command survives.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[A-Za-z0-9+/=\s\\n]{0,16000}"
            r"(?:-----END [A-Z ]{0,40}PRIVATE KEY-----)?"
        ),
    ),
    ("GitHub token", re.compile(r"\b(?:gh[opsu]|github_pat)_[A-Za-z0-9_]{20,255}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,255}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Grafana token", re.compile(r"\bgl(?:c|sa)_[A-Za-z0-9+/=_-]{20,512}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,255}")),
    ("credentials in URL", re.compile(r"(?<=://)[^/\s:@]{1,255}:[^/\s@]{1,255}(?=@)")),
    (
        "authorization header",
        re.compile(r"(?i)(?<=\bbearer )[A-Za-z0-9._~+/=-]{8,4096}|(?<=\bbasic )[A-Za-z0-9+/=]{8,4096}"),
    ),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b[A-Za-z0-9_-]{0,64}(?:password|passwd|secret|token|api[_-]?key)"
            r"[\"']?\s{0,4}[=:]\s{0,4}[\"']?([^\s\"'&;,]{6,4096})"
        ),
    ),
    (
        "secret flag",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])--?[A-Za-z0-9-]{0,32}(?:password|passwd|secret|token|api-?key|auth)[A-Za-z0-9-]{0,16}"
            r"\s{1,4}[\"']?([^\s\"'-][^\s\"']{3,4096})"
        ),
    ),
    (
        "user:password argument",
        re.compile(
            r"(?<![A-Za-z0-9-])(?:-u|--user)\s{1,4}"
            r"(?:'[^:']{1,255}:([^']{1,4096})'|\"[^:\"]{1,255}:([^\"]{1,4096})\"|[^\s:\"']{1,255}:([^\s\"']{1,4096}))"
        ),
    ),
)
OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9+=_-]{32,}$")
OPAQUE_ENTROPY_BITS = 4.0
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY)")
MIN_ENV_SECRET_LENGTH = 8
MAX_STRING = 2000
_MARK = "\x00"
_PARKED = re.compile("\x00(\\d+)\x00")
# Nothing longer than this is ever scanned: the rest is dropped before any pattern runs.
MAX_SCAN = 8000
# Fields that hold file content or free text. They are recorded as size and hash, never as text.
CONTENT_KEYS = frozenset({"content", "new_string", "old_string", "new_source", "edits", "prompt", "todos"})


def entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def secret_label(value: str) -> str | None:
    """Name the kind of secret a whole value looks like, or None."""
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(value):
            return label
    if OPAQUE_TOKEN.match(value) and entropy(value) >= OPAQUE_ENTROPY_BITS:
        return "opaque token"
    return None


def env_secrets(env: dict[str, str] | None = None, extra_names: list[str] | None = None) -> dict[str, str]:
    """Values of environment variables that hold credentials, keyed by value.

    A variable counts if the site names it (extra_names) or if its own name
    says it is a secret. Short values are skipped: replacing them would mangle
    unrelated text.
    """
    env = dict(os.environ) if env is None else env
    wanted = set(extra_names or [])
    found = {}
    for name, value in env.items():
        if len(value) >= MIN_ENV_SECRET_LENGTH and (name in wanted or SECRET_ENV_NAME.search(name)):
            found[value] = name
    return found


def redact_text(text: str, env_values: dict[str, str] | None = None) -> str:
    """Replace anything secret-shaped in a string.

    The string is cut to MAX_SCAN before any pattern runs, so the cost is
    bounded whatever the input, then shortened to MAX_STRING for storage. The
    digest is of the original, so a cut string can still be identified.
    """
    original_length = len(text)
    digest = hashlib.sha256(text.encode()).hexdigest() if original_length > MAX_STRING else ""
    text = text[:MAX_SCAN]
    # Each redaction is parked behind a marker no pattern can produce, so a later
    # pattern never chews on an earlier pattern's replacement.
    parked: list[str] = []

    def park(label: str) -> str:
        parked.append(f"[REDACTED:{label}]")
        return f"{_MARK}{len(parked) - 1}{_MARK}"

    def replace(match: re.Match[str], label: str) -> str:
        secret = next((group for group in match.groups() if group), None) if match.re.groups else match.group(0)
        if not secret:
            return match.group(0)
        if _MARK in secret:
            return match.group(0)
        return match.group(0).replace(secret, park(label))

    for value, name in sorted((env_values or {}).items(), key=lambda item: -len(item[0])):
        if value in text:
            text = text.replace(value, park(f"env {name}"))
    for label, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match, lab=label: replace(match, lab), text)
    text = _PARKED.sub(lambda match: parked[int(match.group(1))], text)
    if original_length > MAX_STRING:
        text = f"{text[:MAX_STRING]}…[truncated; original {original_length} chars, sha256 {digest}]"
    return text


def digest_only(node: Any) -> dict[str, Any]:
    """Stand-in for content that is never stored: its size and hash."""
    text = node if isinstance(node, str) else repr(node)
    return {"omitted": "content", "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def redact(node: Any, env_values: dict[str, str] | None = None) -> Any:
    """Redact every string in a JSON-like structure. Keys are kept; content fields become a digest."""
    if isinstance(node, dict):
        return {
            key: digest_only(value) if key in CONTENT_KEYS else redact(value, env_values) for key, value in node.items()
        }
    if isinstance(node, list):
        return [redact(value, env_values) for value in node]
    if isinstance(node, str):
        return redact_text(node, env_values)
    return node


def find_secrets_in_text(text: str) -> list[str]:
    """Labels of every secret shape still present in a string. Used as a last check before a log is published."""
    return [label for label, pattern in SECRET_PATTERNS if pattern.search(text[: MAX_SCAN * 8])]
