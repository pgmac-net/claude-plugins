"""In-memory stand-ins for a chat platform, an agent and the tracker."""

# The fakes keep the interfaces' signatures, whether or not they use every argument.
# pylint: disable=unused-argument

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sre_gateway.agent import AskPermission, Event, PermissionAnswer, TurnEnd
from sre_gateway.chat import ApprovalView, FileRef, FileUnavailable, NameTaken, Place, Posted, UploadRefused
from sre_gateway.indicator import NAME

INDICATOR_PREFIX = f"{NAME} is "


@dataclass
class FakeChat:  # pylint: disable=too-many-instance-attributes
    posts: list[tuple[Place, str]] = field(default_factory=list)
    approvals: list[tuple[Place, ApprovalView, Posted]] = field(default_factory=list)
    closed: dict[str, str] = field(default_factory=dict)
    told: list[tuple[str, str]] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    invited: dict[str, list[str]] = field(default_factory=dict)
    archived: list[str] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    fail_create: bool = False
    # Attachments: file id -> content, file id -> why it cannot be fetched, and the ids fetched.
    files: dict[str, bytes] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)
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

    # The working indicator's lines, kept apart from posts so tests of what the gateway says are unchanged.
    lines: list[tuple[Place, str, str]] = field(default_factory=list)  # (place, ts, text) as posted
    edits: list[tuple[str, str]] = field(default_factory=list)  # (ts, text)
    deleted: list[str] = field(default_factory=list)
    typing: list[tuple[Place, bool]] = field(default_factory=list)
    # Everything posted, indicator lines and typing included, in order: (kind, text, place).
    timeline: list[tuple[str, str, Place]] = field(default_factory=list)
    fail_indicator: bool = False

    async def post(self, place: Place, text: str, raw: bool = False) -> Posted:
        posted = Posted(place.channel, self._next())
        if text.startswith(INDICATOR_PREFIX):
            if self.fail_indicator:
                raise RuntimeError("platform error")
            self.lines.append((place, posted.ts, text))
            self.timeline.append(("line", text, place))
            return posted
        self.posts.append((place, text))
        self.timeline.append(("post", text, place))
        if raw:
            self.raw_posts.append(text)
        return posted

    async def update(self, posted: Posted, text: str) -> None:
        if self.fail_indicator:
            raise RuntimeError("platform error")
        self.edits.append((posted.ts, text))

    async def delete(self, posted: Posted) -> None:
        if self.fail_indicator:
            raise RuntimeError("platform error")
        self.deleted.append(posted.ts)

    async def set_typing(self, place: Place, on: bool) -> None:
        if self.fail_indicator:
            raise RuntimeError("platform error")
        self.typing.append((place, on))
        self.timeline.append(("typing", "on" if on else "off", place))

    def live_lines(self) -> list[str]:
        """Indicator lines posted and not deleted."""
        return [ts for _place, ts, _text in self.lines if ts not in self.deleted]

    def escape(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def permalink(self, posted: Posted) -> str | None:
        return f"https://chat.example.com/{posted.channel}/{posted.ts}"

    async def post_approval(self, place: Place, view: ApprovalView) -> Posted:
        posted = Posted(place.channel, self._next())
        self.approvals.append((place, view, posted))
        self.timeline.append(("approval", view.tool, place))
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

    async def download(self, file: FileRef, max_bytes: int) -> bytes:
        self.downloads.append(file.id)
        if file.id in self.unavailable:
            raise FileUnavailable(self.unavailable[file.id])
        return self.files[file.id][: max_bytes + 1]

    # Images posted: (place, filename, title, bytes); refuse_upload makes the platform say no.
    uploads: list[tuple[Place, str, str, bytes]] = field(default_factory=list)
    refuse_upload: str | None = None

    async def upload(self, place: Place, data: bytes, filename: str, title: str) -> str | None:
        if self.refuse_upload:
            raise UploadRefused(self.refuse_upload)
        self.uploads.append((place, filename, title, data))
        self.timeline.append(("upload", title, place))
        return f"https://chat.example.com/files/{filename}"

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
        if self.log.get("fail_starts", 0) > 0:
            self.log["fail_starts"] -= 1
            raise RuntimeError("Control request timeout: initialize")
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
    def make(ask: AskPermission, triage: bool = False, question: bool = False) -> FakeDriver:
        log.setdefault("policies", []).append("question" if question else "triage-only" if triage else "chat")
        return FakeDriver(ask, script, log)

    return make


async def settle(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)
