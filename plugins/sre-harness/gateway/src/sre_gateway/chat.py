"""The chat platform interface. Slack implements it; tests use a fake.

Everything the gateway needs from a platform is here, in platform-neutral
terms, so another platform (Zulip, Discord, Teams) is one new adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Place:
    """Where a message goes: a channel, and optionally a thread within it."""

    channel: str
    thread: str | None = None


@dataclass(frozen=True)
class Posted:
    """A message the gateway posted, for editing or linking later."""

    channel: str
    ts: str


@dataclass(frozen=True)
class Incoming:
    """A message from a person. 'mentions_bot' is true when it addresses the gateway."""

    channel: str
    user: str
    text: str
    ts: str
    thread: str | None = None
    mentions_bot: bool = False


@dataclass(frozen=True)
class Command:
    """A slash command such as '/incident <text>'."""

    name: str
    user: str
    channel: str
    text: str


@dataclass(frozen=True)
class Click:
    """An answer to an approval request: 'approve' or 'deny', with an optional reason."""

    request_id: str
    user: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class ApprovalView:
    """What an approval request shows."""

    request_id: str
    tool: str
    summary: str
    statement_link: str | None
    expires_in_seconds: int
    description: str | None = None


class Handler(Protocol):
    async def on_message(self, message: Incoming) -> None: ...

    async def on_command(self, command: Command) -> None: ...

    async def on_click(self, click: Click) -> None: ...


class ChatAdapter(Protocol):
    """A chat platform. Every method may raise; the gateway reports and carries on."""

    async def start(self, handler: Handler) -> None:
        """Connect outbound and deliver events to the handler until stop() is called."""

    async def stop(self) -> None: ...

    async def post(self, place: Place, text: str, raw: bool = False) -> Posted:
        """Post text. It is escaped so it cannot mention, link or format; raw is for text the gateway composed
        itself, with every part that came from elsewhere passed through escape() first."""

    def escape(self, text: str) -> str: ...

    async def permalink(self, posted: Posted) -> str | None: ...

    async def post_approval(self, place: Place, view: ApprovalView) -> Posted: ...

    async def close_approval(self, posted: Posted, view: ApprovalView, outcome: str) -> None:
        """Replace the request's buttons with its outcome, so it cannot be answered again."""

    async def tell(self, place: Place, user: str, text: str) -> None:
        """Say something only one user sees."""

    async def create_private_channel(self, name: str) -> str:
        """Create a private channel and return its ID. Raises NameTaken if the name is in use."""

    async def invite(self, channel: str, users: list[str]) -> None: ...

    async def archive(self, channel: str) -> None: ...

    async def user_name(self, user: str) -> str: ...

    def channel_link(self, channel: str) -> str:
        """How to refer to a channel inside a message."""


class NameTaken(Exception):
    """A channel with that name already exists."""


Notify = Callable[[str], Awaitable[None]]
