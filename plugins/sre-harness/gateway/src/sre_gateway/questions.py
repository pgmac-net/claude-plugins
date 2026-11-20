"""The question log: the gateway's own record of one question asked with /sre.

A question has no incident, so the audit hook records nothing for it, and Pi
has no hook at all. The gateway sees every tool call and its output, so it
keeps the record itself: who asked, what, each call (input redacted, output as
size and hash only) and each refusal. One chained JSON-lines file per
question, readable only by the gateway's user, kept for RETENTION_DAYS and
never committed (ADR 0022).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sre_gateway import harness

log = logging.getLogger(__name__)

RETENTION_DAYS = 30
GENESIS = "0" * 64
TEXT_LIMIT = 2000


def new_id() -> str:
    """'q-<UTC time>-<random>': sorts by time, and two questions in one second still differ."""
    return f"q-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


class QuestionLog:
    """Append-only, hash-chained records for one question."""

    def __init__(self, directory: Path, question_id: str, env_values: dict[str, str] | None = None):
        self.id = question_id
        self.path = directory / f"{question_id}.jsonl"
        self._env_values = env_values or {}
        self._previous = GENESIS

    def _text(self, text: str) -> str:
        return harness.redact.redact_text(text, self._env_values, limit=TEXT_LIMIT, digest=False)

    def _append(self, event: str, **fields: Any) -> None:
        record = {
            "v": 1,
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "question": self.id,
            "capture": "gateway",
            "event": event,
            **fields,
            "prev": self._previous,
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(handle, (line + "\n").encode("utf-8"))
        finally:
            os.close(handle)
        self._previous = hashlib.sha256(line.encode()).hexdigest()

    def asked(self, user: str, text: str, agent: str | None = None) -> None:
        """The first record. Raises if it cannot be written: a question that left no record is not asked."""
        self._append("asked", user=user, text=self._text(text), **({"agent": agent} if agent else {}))

    def message(self, user: str, text: str) -> None:
        self._append("message", user=user, text=self._text(text))

    def attempt(self, tool: str, tool_input: dict[str, Any], tool_use_id: str) -> None:
        self._append(
            "attempt",
            tool=tool,
            input=harness.redact.redact(tool_input, self._env_values),
            tool_use_id=tool_use_id[:128],
        )

    def complete(self, tool_use_id: str, output: str, is_error: bool) -> None:
        data = output.encode()
        self._append(
            "complete",
            tool_use_id=tool_use_id[:128],
            output={"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
            outcome={"is_error": is_error},
        )

    def refused(self, tool: str, tool_input: dict[str, Any], tool_use_id: str) -> None:
        self._append(
            "refused",
            tool=tool,
            input=harness.redact.redact(tool_input, self._env_values),
            tool_use_id=tool_use_id[:128],
        )

    def ended(self, reason: str) -> None:
        self._append("ended", reason=reason)


def verify(path: Path) -> bool:
    """Whether each line of a question log carries the hash of the line before it."""
    previous = GENESIS
    for line in path.read_text(encoding="utf-8").splitlines():
        if json.loads(line).get("prev") != previous:
            return False
        previous = hashlib.sha256(line.encode()).hexdigest()
    return True


def prune(directory: Path, days: int = RETENTION_DAYS, now: float | None = None) -> int:
    """Remove question logs last written more than `days` ago. Returns how many went."""
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for path in directory.glob("q-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            log.exception("could not prune %s", path.name)
    return removed
