"""The incident tracker, as the gateway uses it: copy a chat message to the issue, ask whether it is closed."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from sre_gateway import harness


class Tracker(Protocol):
    async def comment(self, incident: str, body: str) -> None: ...

    async def is_closed(self, incident: str) -> bool: ...


class GhTracker:
    """The tracking issue on the code host, through the gh CLI the harness already uses."""

    def __init__(self, doc: dict[str, Any]):
        self._doc = doc

    async def comment(self, incident: str, body: str) -> None:
        repo, number = harness.sre_common.split_incident_id(incident, self._doc)
        await asyncio.to_thread(
            harness.sre_common.run_gh,
            ["issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
            body,
            60,
        )

    async def is_closed(self, incident: str) -> bool:
        repo, number = harness.sre_common.split_incident_id(incident, self._doc)
        out = await asyncio.to_thread(
            harness.sre_common.run_gh, ["issue", "view", str(number), "--repo", repo, "--json", "state"], None, 60
        )
        return json.loads(out).get("state") == "CLOSED"
