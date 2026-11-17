"""In-memory stand-ins for a chat platform, an agent and the tracker."""

# The fakes keep the interfaces' signatures, whether or not they use every argument.
# pylint: disable=unused-argument

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sre_gateway.agent import AskPermission, Event, PermissionAnswer, TurnEnd
from sre_gateway.chat import ApprovalView, NameTaken, Place, Posted


@dataclass
class FakeChat:
    posts: list[tuple[Place, str]] = field(default_factory=list)
    approvals: list[tuple[Place, ApprovalView, Posted]] = field(default_factory=list)
    closed: dict[str, str] = field(default_factory=dict)
    told: list[tuple[str, str]] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    invited: dict[str, list[str]] = field(default_factory=dict)
    archived: list[str] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    fail_create: bool = False
    handler: Any = None
    _ts: int = 0

    def _next(self) -> str:
        self._ts += 1
        return f"{self._ts}.000"

    async def start(self, handler: Any) -> None:  # pragma: no cover - not used by tests
        self.handler = handler

    async def stop(self) -> None:
        return None

    raw_posts: list[str] = field(default_factory=list)

    async def post(self, place: Place, text: str, raw: bool = False) -> Posted:
        self.posts.append((place, text))
        if raw:
            self.raw_posts.append(text)
        return Posted(place.channel, self._next())

    def escape(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def permalink(self, posted: Posted) -> str | None:
        return f"https://chat.example.com/{posted.channel}/{posted.ts}"

    async def post_approval(self, place: Place, view: ApprovalView) -> Posted:
        posted = Posted(place.channel, self._next())
        self.approvals.append((place, view, posted))
        return posted

    async def close_approval(self, posted: Posted, view: ApprovalView, outcome: str) -> None:
        self.closed[view.request_id] = outcome

    async def tell(self, place: Place, user: str, text: str) -> None:
        self.told.append((user, text))

    async def create_private_channel(self, name: str) -> str:
        if self.fail_create:
            raise RuntimeError("platform error")
        if name in self.taken:
            raise NameTaken(name)
        self.channels.append(name)
        return f"C{len(self.channels):08d}"

    async def invite(self, channel: str, users: list[str]) -> None:
        self.invited[channel] = list(users)

    async def archive(self, channel: str) -> None:
        self.archived.append(channel)

    async def user_name(self, user: str) -> str:
        return f"name-{user}"

    def channel_link(self, channel: str) -> str:
        return f"#{channel}"

    def texts(self) -> list[str]:
        return [text for _place, text in self.posts]


@dataclass
class FakeTracker:
    comments: list[tuple[str, str]] = field(default_factory=list)
    closed: set[str] = field(default_factory=set)
    fail: bool = False

    async def comment(self, incident: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("gh failed")
        self.comments.append((incident, body))

    async def is_closed(self, incident: str) -> bool:
        return incident in self.closed


class FakeDriver:
    """Plays a script: each turn yields the next list of events, or calls the permission callback."""

    def __init__(self, ask: AskPermission, script: list[list[Any]], log: dict[str, Any]):
        self.ask = ask
        self.script = script
        self.log = log
        self.session_id: str | None = None

    async def start(self, resume: str | None) -> None:
        self.log.setdefault("starts", []).append(resume)
        self.session_id = resume or "session-1"

    async def turn(self, message: str):
        self.log.setdefault("messages", []).append(message)
        events = self.script.pop(0) if self.script else []
        for item in events:
            if isinstance(item, tuple) and item[0] == "ask":
                answer: PermissionAnswer = await self.ask(item[1], item[2], item[3])
                self.log.setdefault("answers", []).append(answer)
                continue
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                continue
            yield item
        yield TurnEnd(self.session_id)

    async def stop(self) -> None:
        self.log["stops"] = self.log.get("stops", 0) + 1


def driver_factory(script: list[list[Event | tuple]], log: dict[str, Any]):
    def make(ask: AskPermission, triage: bool = False) -> FakeDriver:
        log.setdefault("policies", []).append("triage-only" if triage else "chat")
        return FakeDriver(ask, script, log)

    return make


async def settle(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)
