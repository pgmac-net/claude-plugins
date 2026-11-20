"""The gateway's link to the sre-harness plugin it ships with.

The gateway reuses the plugin's own modules (site loading, validation, tool
policy, redaction, audit records) rather than restating them, so both always
agree. The plugin is the directory three levels above this package, unless
SRE_HARNESS_PLUGIN names another.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(os.environ.get("SRE_HARNESS_PLUGIN") or Path(__file__).resolve().parents[3]).resolve()
SCRIPTS = PLUGIN_ROOT / "scripts"
if not (SCRIPTS / "sre_common.py").is_file():
    raise ImportError(f"no sre-harness scripts at {SCRIPTS}; set SRE_HARNESS_PLUGIN to the plugin directory")
sys.path.insert(0, str(SCRIPTS))

# pylint: disable=wrong-import-position,import-error
import alerts  # noqa: E402
import audit  # noqa: E402
import audit_hook  # noqa: E402
import chat_policy  # noqa: E402
import redact  # noqa: E402
import sre_common  # noqa: E402
import tool_policy  # noqa: E402
import validate_site  # noqa: E402

__all__ = [
    "PLUGIN_ROOT",
    "SCRIPTS",
    "alerts",
    "audit",
    "audit_hook",
    "chat_policy",
    "redact",
    "sre_common",
    "tool_policy",
    "validate_site",
]

INCIDENT_CALL = re.compile(r"(?:^|\s)\S*/scripts/incident\.py\s+(open|attach|close|promote|dismiss)\b")
RESULT_LINE = re.compile(r'^\{"id":.*\}$', re.MULTILINE)


def incident_call(command: str) -> str | None:
    """Which incident.py subcommand a shell command runs: open, attach, close, promote or dismiss."""
    match = INCIDENT_CALL.search(command)
    return match.group(1) if match else None


def opened_incident(output: str) -> dict[str, Any] | None:
    """The incident an 'incident.py open' or 'attach' run reports, confirmed against local state.

    The output alone is not trusted: the id must name a local incident state
    file that incident.py wrote, and the URL must match it.
    """
    for line in RESULT_LINE.findall(output):
        try:
            reported = json.loads(line)
        except ValueError:
            continue
        incident_id, url = reported.get("id"), reported.get("url")
        if not isinstance(incident_id, str) or not sre_common.INCIDENT_ID.match(incident_id):
            continue
        path = sre_common.incident_state_path(incident_id)
        if not path.is_file():
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("url") == url:
            return state
    return None


def outbound_secrets(extra: dict[str, str] | None = None, names: list[str] | None = None) -> dict[str, str]:
    """Values to scrub from every message the gateway posts: credential variables and its own tokens."""
    found = redact.env_secrets(extra_names=names or [])
    for name, value in (extra or {}).items():
        if value and len(value) >= redact.MIN_ENV_SECRET_LENGTH:
            found[value] = name
    return found


def scrub(text: str, secrets: dict[str, str], limit: int | None = None) -> str:
    """Redact a message before it leaves the machine. No digest: a hash of the original is information about it."""
    return redact.redact_text(text, secrets, limit=limit, digest=False)
