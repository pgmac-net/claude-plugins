#!/usr/bin/env python3
"""Audit hook: record every tool call made during an incident.

Runs on PreToolUse (an attempt) and PostToolUse (a completion). It is not
written by the model and the model cannot skip it. It records nothing unless
the session is bound to an incident, and it never blocks a tool call: every
path exits 0 with no output.

Standard library only, so it starts fast and cannot fail for want of a
package. A session is bound to an incident when the hook sees that session
run `incident.py open` or `incident.py attach` successfully.

Each record is one JSON line. Tool output is stored as size and SHA-256 only.
Records are chained: each carries the hash of the line before it, so an edit
or deletion is detectable.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact  # noqa: E402  pylint: disable=wrong-import-position

EVENTS = {"PreToolUse": "attempt", "PostToolUse": "complete"}
BINDING_COMMAND = re.compile(r"incident\.py\b.*\b(open|attach)\b", re.DOTALL)
INCIDENT_ID = re.compile(r'"id":\s*"([a-z][a-z0-9_-]*-\d+)"')
GENESIS = "0" * 64


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "sre-harness"


def spool_path(incident_id: str) -> Path:
    return state_dir() / "audit" / f"{incident_id}.jsonl"


def bound_incident(session_id: str) -> str | None:
    path = state_dir() / "sessions" / f"{session_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("incident")
    except (OSError, ValueError):
        return None


def bind(session_id: str, incident_id: str) -> None:
    path = state_dir() / "sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"incident": incident_id}) + "\n", encoding="utf-8")


def response_text(response: Any) -> str:
    """Flatten a tool response for hashing and for finding an incident id."""
    return response if isinstance(response, str) else json.dumps(response, sort_keys=True, default=str)


def binding_from(payload: dict[str, Any]) -> str | None:
    """The incident this call opened or attached to, if it did."""
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not BINDING_COMMAND.search(command):
        return None
    match = INCIDENT_ID.search(response_text(payload.get("tool_response")).replace('\\"', '"'))
    return match.group(1) if match else None


def credential_names(incident_id: str) -> list[str]:
    """Environment variable names the site says hold credentials, saved by incident.py."""
    path = state_dir() / "incidents" / f"{incident_id}.json"
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("redact_env", []))
    except (OSError, ValueError):
        return []


def build_record(payload: dict[str, Any], incident_id: str, capture: str = "hook") -> dict[str, Any]:
    """One audit record from a hook payload. Inputs are redacted; output is hashed, not kept."""
    env_values = redact.env_secrets(extra_names=credential_names(incident_id))
    record: dict[str, Any] = {
        "v": 1,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "incident": incident_id,
        "session": payload.get("session_id"),
        "capture": capture,
        "event": EVENTS.get(str(payload.get("hook_event_name")), "unknown"),
        "tool": payload.get("tool_name"),
        "input": redact.redact(payload.get("tool_input") or {}, env_values),
    }
    if record["event"] == "complete":
        response = payload.get("tool_response")
        text = response_text(response)
        record["output"] = {"bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}
        if isinstance(response, dict):
            flags = {key: response[key] for key in ("interrupted", "is_error", "success") if key in response}
            if flags:
                record["outcome"] = flags
    return record


def append(incident_id: str, record: dict[str, Any]) -> None:
    """Append one chained record under an exclusive lock."""
    path = spool_path(incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        lines = handle.read().splitlines()
        record["prev"] = hashlib.sha256(lines[-1].encode()).hexdigest() if lines else GENESIS
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def is_closed(incident_id: str) -> bool:
    return (state_dir() / "audit" / f"{incident_id}.closed").exists()


def handle(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id or payload.get("hook_event_name") not in EVENTS:
        return
    incident_id = bound_incident(session_id)
    opened = binding_from(payload)
    if opened and opened != incident_id:
        bind(session_id, opened)
        incident_id = opened
    if incident_id and not is_closed(incident_id):
        append(incident_id, build_record(payload, incident_id))


def main() -> int:
    """Never raise, never print, never block a tool call."""
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        handle(payload)
    except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
        try:
            incident_id = bound_incident(str(payload.get("session_id") or ""))
            if incident_id:
                append(
                    incident_id,
                    {
                        "v": 1,
                        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "incident": incident_id,
                        "session": payload.get("session_id"),
                        "capture": "hook",
                        "event": "capture_error",
                        "tool": payload.get("tool_name"),
                        "error": type(exc).__name__,
                    },
                )
        except Exception:  # noqa: BLE001, S110  pylint: disable=broad-exception-caught
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
