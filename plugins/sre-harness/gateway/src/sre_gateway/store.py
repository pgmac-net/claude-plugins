"""Which chat conversation belongs to which incident and agent session, kept across restarts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Conversation:  # pylint: disable=too-many-instance-attributes  # a persisted record
    key: str
    channel: str
    thread: str | None = None
    incident: str | None = None
    session_id: str | None = None
    drill: bool = False
    title: str = ""
    started_by: str = ""
    archived: bool = False
    # Started by the alert source rather than a person.
    auto: bool = False
    # 'triage-only' until a responder first speaks, then 'chat'; never back.
    policy: str = "chat"
    # A triage-only session's token: the harness scripts let it write only to the record it opened.
    triage_token: str = ""
    # The agent has closed or dismissed the incident; its channel stays until the archive sweep.
    ended: bool = False


class Store:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Conversation]:
        if not self.path.is_file():
            return []
        known = {f.name for f in fields(Conversation)}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [Conversation(**{k: v for k, v in item.items() if k in known}) for item in raw.get("conversations", [])]

    def save(self, conversations: list[Conversation]) -> None:
        data = json.dumps({"v": 1, "conversations": [asdict(c) for c in conversations]}, indent=2) + "\n"
        write_private(self.path, data)


def write_private(path: Path, data: str) -> None:
    """Write a file atomically, in a directory and with a mode only this user can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(data)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
