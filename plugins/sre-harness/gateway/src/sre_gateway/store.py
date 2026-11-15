"""Which chat conversation belongs to which incident and agent session, kept across restarts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Conversation:
    key: str
    channel: str
    thread: str | None = None
    incident: str | None = None
    session_id: str | None = None
    drill: bool = False
    title: str = ""
    started_by: str = ""
    archived: bool = False


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
        """Write atomically, readable by this user only."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        data = json.dumps({"v": 1, "conversations": [asdict(c) for c in conversations]}, indent=2) + "\n"
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".conversations-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(data)
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
