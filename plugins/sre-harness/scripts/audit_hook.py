#!/usr/bin/env python3
"""Audit hook: record every tool call made during an incident.

Runs on PreToolUse (an attempt) and PostToolUse (a completion). It is not
written by the model and the model cannot skip it. It records nothing unless
the session is bound to an incident, and it never blocks a tool call: every
path exits 0 with no output.

Standard library only, so it starts fast and cannot fail for want of a
package. A session is bound to an incident when its tool output carries a
single-use bind token that incident.py wrote to the state directory: the
incident id is read from that file, never from the output.

Each record is one JSON line. Tool output is stored as size and SHA-256 only.
Records are chained: each carries the hash of the line before it.

What this does and does not guarantee: the log, the bind tokens and the
marker that switches capture on are files owned by the same user the agent
runs as. An agent that sets out to evade or rewrite them can. The chain and
the head hashes anchored in timeline comments make accidental damage and
after-the-fact edits visible; they are not a defence against a hostile
process on the same account. Shipping records off-host as they are written
is the control for that, and is not part of this hook.
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
BIND_TOKEN = re.compile(r'"bind":\s*"([0-9a-f]{32})"')
INCIDENT_ID = re.compile(r"^[a-z][a-z0-9_-]*-\d+$")
TAIL_CHUNK = 65536
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
    """The incident this call opened or attached to, if it did.

    incident.py prints a random single-use token and stores the incident id
    under it. Only a token that names such a file binds, and the id comes from
    the file. Printing an id-shaped string binds nothing.
    """
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Bash":
        return None
    match = BIND_TOKEN.search(response_text(payload.get("tool_response")).replace('\\"', '"'))
    if not match:
        return None
    token_file = state_dir() / "binds" / match.group(1)
    try:
        incident_id = token_file.read_text(encoding="utf-8").strip()
        token_file.unlink()
    except OSError:
        return None
    return incident_id if INCIDENT_ID.match(incident_id) else None


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


def last_line(handle) -> str:
    """The final line of an open file, read from the end so cost does not grow with the log."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return ""
    data = b""
    position = size
    while position > 0:
        step = min(TAIL_CHUNK, position)
        position -= step
        handle.seek(position)
        data = handle.read(step) + data
        stripped = data.rstrip(b"\n")
        if b"\n" in stripped:
            return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
    return data.rstrip(b"\n").decode("utf-8")


def append(incident_id: str, record: dict[str, Any]) -> None:
    """Append one chained record under an exclusive lock."""
    path = spool_path(incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        previous = last_line(handle)
        record["prev"] = hashlib.sha256(previous.encode()).hexdigest() if previous else GENESIS
        handle.seek(0, os.SEEK_END)
        handle.write((json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))


def head(incident_id: str) -> tuple[int, str] | None:
    """(record count, hash of the last line) of an incident's log, or None if it has none."""
    path = spool_path(incident_id)
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    return len(lines), hashlib.sha256(lines[-1].encode()).hexdigest()


def is_closed(incident_id: str) -> bool:
    return (state_dir() / "audit" / f"{incident_id}.closed").exists()


def handle(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id or payload.get("hook_event_name") not in EVENTS:
        return
    incident_id = bound_incident(session_id)
    opened = binding_from(payload)
    if opened and opened != incident_id:
        if incident_id and not is_closed(incident_id):
            # Moving a live session to another incident is recorded where it was, so it cannot go unseen.
            append(
                incident_id,
                {
                    "v": 1,
                    "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "incident": incident_id,
                    "session": session_id,
                    "capture": "hook",
                    "event": "rebind",
                    "to": opened,
                },
            )
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
