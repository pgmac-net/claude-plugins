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

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----)?", re.DOTALL),
    ),
    ("GitHub token", re.compile(r"\b(?:gh[opsu]|github_pat)_[A-Za-z0-9_]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Grafana token", re.compile(r"\bgl(?:c|sa)_[A-Za-z0-9+/=_-]{20,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("credentials in URL", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("bearer token", re.compile(r"(?i)(?<=\bbearer )[A-Za-z0-9._~+/=-]{16,}")),
    (
        "secret assignment",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key))"
            r"[\"']?\s*[=:]\s*[\"']?([^\s\"'&;,]{6,})"
        ),
    ),
)
OPAQUE_TOKEN = re.compile(r"^[A-Za-z0-9+=_-]{32,}$")
OPAQUE_ENTROPY_BITS = 4.0
SECRET_ENV_NAME = re.compile(r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY)")
MIN_ENV_SECRET_LENGTH = 8
MAX_STRING = 2000


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
    """Replace anything secret-shaped in a string, then truncate it if very long."""
    for value, name in sorted((env_values or {}).items(), key=lambda item: -len(item[0])):
        text = text.replace(value, f"[REDACTED:env {name}]")
    for label, pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m, lab=label: m.group(0).replace(m.group(1), f"[REDACTED:{lab}]"), text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    if len(text) > MAX_STRING:
        digest = hashlib.sha256(text.encode()).hexdigest()
        text = f"{text[:MAX_STRING]}…[truncated {len(text) - MAX_STRING} chars, sha256 of redacted text {digest}]"
    return text


def redact(node: Any, env_values: dict[str, str] | None = None) -> Any:
    """Redact every string in a JSON-like structure. Keys are kept."""
    if isinstance(node, dict):
        return {key: redact(value, env_values) for key, value in node.items()}
    if isinstance(node, list):
        return [redact(value, env_values) for value in node]
    if isinstance(node, str):
        return redact_text(node, env_values)
    return node
