"""The gateway: routes chat to agent sessions and back.

One conversation per incident. It starts as a thread in the hub channel and,
once the agent has opened the tracking issue, moves to a private channel of
its own with the responders invited. Each conversation has one resumable
agent session that handles one message at a time; sessions stop when idle
and resume on the next message, and only a few run at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sre_gateway import harness, prompts
from sre_gateway.agent import (
    AgentDriver,
    AskPermission,
    Notice,
    PermissionAnswer,
    Text,
    ToolCall,
    ToolOutput,
    TurnEnd,
)
from sre_gateway.approvals import ApprovalBroker, Recorder
from sre_gateway.chat import ChatAdapter, Click, Command, Incoming, NameTaken, Place, Posted
from sre_gateway.store import Conversation, Store
from sre_gateway.tracker import Tracker

log = logging.getLogger(__name__)

CHUNK = 3500
ARCHIVE_CHECK_SECONDS = 3600
MAX_TRACKED_CALLS = 200
STATUS_WORDS = frozenset({"status", "!status"})


@dataclass(frozen=True)
class Settings:
    hub: str
    responders: frozenset[str]
    approvers: frozenset[str]
    approval_ttl: int
    idle_seconds: int
    max_sessions: int
    state_dir: Path


@dataclass
class Session:
    conv: Conversation
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    driver: AgentDriver | None = None
    busy: bool = False
    last_text: Posted | None = None
    last_problem: str | None = None
    calls: dict[str, ToolCall] = field(default_factory=dict)
    worker: asyncio.Task | None = None

    @property
    def place(self) -> Place:
        return Place(self.conv.channel, self.conv.thread)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split a long message at line breaks where possible; a line longer than size is cut."""
    pieces = [line[i : i + size] for line in text.splitlines(keepends=True) for i in range(0, len(line), size)]
    parts, current = [], ""
    for piece in pieces:
        if len(current) + len(piece) > size:
            parts.append(current)
            current = ""
        current += piece
    if current:
        parts.append(current)
    return parts or [""]


def channel_name(incident: dict[str, Any], suffix: int = 0) -> str:
    """'inc-<number>-<slug>', or 'drill-…' for a drill; lower case, at most 80 characters."""
    prefix = "drill" if incident.get("drill") else "inc"
    title = re.sub(r"^\[drill\]\s*", "", str(incident.get("title", "")), flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "incident"
    name = f"{prefix}-{incident['number']}-{slug}"
    return (f"{name[:76]}-{suffix}" if suffix else name)[:80]


class Gateway:  # pylint: disable=too-many-instance-attributes
    def __init__(  # noqa: PLR0913  pylint: disable=too-many-arguments
        self,
        settings: Settings,
        *,
        chat: ChatAdapter,
        tracker: Tracker,
        driver_factory: Callable[[AskPermission], AgentDriver],
        record: Recorder,
        scrub: Callable[[str], str],
    ):
        self.settings = settings
        self.chat = chat
        self.tracker = tracker
        self._driver_factory = driver_factory
        self._scrub = scrub
        self.broker = ApprovalBroker(
            chat, approvers=settings.approvers, ttl_seconds=settings.approval_ttl, record=record, scrub=scrub
        )
        self.store = Store(settings.state_dir / "conversations.json")
        self.sessions: dict[str, Session] = {}
        self._slots = asyncio.Semaphore(settings.max_sessions)
        self._live = 0
        self._archiver: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------------

    def load(self) -> None:
        for conv in self.store.load():
            if not conv.archived:
                self._add(conv)

    async def run(self) -> None:
        self.load()
        self._archiver = asyncio.create_task(self._archive_loop())
        try:
            await self.chat.start(self)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._archiver:
            self._archiver.cancel()
        for session in list(self.sessions.values()):
            if session.worker:
                session.worker.cancel()
            await self._park(session)
        self._save()

    def _add(self, conv: Conversation) -> Session:
        session = Session(conv)
        self.sessions[conv.key] = session
        session.worker = asyncio.create_task(self._work(session))
        return session

    def _save(self) -> None:
        self.store.save([s.conv for s in self.sessions.values()])

    # --- chat events -------------------------------------------------------------

    async def on_command(self, command: Command) -> None:
        if command.name != "incident":
            return
        if command.user not in self.settings.responders:
            await self.chat.tell(Place(command.channel), command.user, "Only listed responders can start an incident.")
            return
        text, drill = self._drill(command.text)
        if not text:
            await self.chat.tell(Place(command.channel), command.user, "Usage: /incident [drill] <what is broken>")
            return
        name = await self._name(command.user)
        kind = "a drill" if drill else "an incident"
        posted = await self.chat.post(Place(self.settings.hub), self._scrub(f"{name} started {kind}: {text}"))
        await self._start(command.user, name, text, Place(self.settings.hub, posted.ts), drill)

    async def on_message(self, message: Incoming) -> None:
        if message.user not in self.settings.responders:
            log.info("ignored a message from a user who is not a responder")
            return
        session = self._find(message)
        if session is not None:
            await self._converse(session, message)
        elif message.channel == self.settings.hub and message.thread is None and message.mentions_bot:
            text, drill = self._drill(message.text)
            if text:
                name = await self._name(message.user)
                await self._start(message.user, name, text, Place(self.settings.hub, message.ts), drill)

    async def on_click(self, click: Click) -> None:
        await self.broker.on_click(click)

    # --- conversations -----------------------------------------------------------

    @staticmethod
    def _drill(text: str) -> tuple[str, bool]:
        text = text.strip()
        if re.match(r"^drill\b", text, re.IGNORECASE):
            return text[5:].strip(" :-"), True
        return text, False

    def _find(self, message: Incoming) -> Session | None:
        for session in self.sessions.values():
            conv = session.conv
            if conv.archived or conv.channel != message.channel:
                continue
            if conv.thread is None or conv.thread == message.thread:
                return session
        return None

    async def _start(  # pylint: disable=too-many-arguments
        self, user: str, name: str, text: str, place: Place, drill: bool
    ) -> None:
        conv = Conversation(
            key=f"{place.channel}:{place.thread}",
            channel=place.channel,
            thread=place.thread,
            drill=drill,
            started_by=user,
        )
        session = self._add(conv)
        self._save()
        await session.queue.put(prompts.first_message(name, text, drill))

    async def _converse(self, session: Session, message: Incoming) -> None:
        text = message.text.strip()
        if text.lower() in STATUS_WORDS:
            await self.chat.post(session.place, self.status(session))
            return
        name = await self._name(message.user)
        if session.conv.incident:
            await self._mirror(session, message, name)
        if session.busy:
            await self.chat.tell(session.place, message.user, "Queued: the agent is still on the previous message.")
        await session.queue.put(f"[chat] {name}: {text}")

    async def _mirror(self, session: Session, message: Incoming, name: str) -> None:
        """Copy a person's message to the tracking issue, so the issue stays the record."""
        link = None
        with contextlib.suppress(Exception):
            link = await self.chat.permalink(Posted(message.channel, message.ts))
        body = f"**{name}** (chat): {self._scrub(message.text.strip())}"
        if link:
            body += f"\n\n[message]({link})"
        try:
            await self.tracker.comment(session.conv.incident or "", body)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not copy a message to the issue")
            session.last_problem = f"copying to the issue failed: {type(exc).__name__}"
            await self.chat.post(session.place, "This message could not be copied to the tracking issue.")

    def status(self, session: Session) -> str:
        conv = session.conv
        if session.busy:
            agent = "working"
        elif session.driver is not None:
            agent = "idle"
        else:
            agent = "stopped (the next message resumes it)" if conv.session_id else "not started"
        lines = [
            f"Incident: {conv.incident or 'not opened yet'}{' (drill)' if conv.drill else ''}",
            f"Agent: {agent}; messages queued: {session.queue.qsize()}",
            f"Approval requests open: {self.broker.open_requests}",
            f"Agent sessions in use: {self._live} of {self.settings.max_sessions}",
        ]
        if session.last_problem:
            lines.append(f"Last problem: {session.last_problem}")
        return "\n".join(lines)

    # --- the agent -----------------------------------------------------------------

    async def _work(self, session: Session) -> None:
        """Feed one session its messages, one turn at a time; stop the agent when idle."""
        while True:
            try:
                if session.driver is not None:
                    message = await asyncio.wait_for(session.queue.get(), self.settings.idle_seconds)
                else:
                    message = await session.queue.get()
            except TimeoutError:
                await self._park(session)
                continue
            if message is None:
                await self._park(session)
                return
            if not await self._ensure_driver(session):
                continue
            await self._turn(session, message)

    async def _ensure_driver(self, session: Session) -> bool:
        if session.driver is not None:
            return True
        if self._slots.locked():
            await self.chat.post(
                session.place, "All agent sessions are in use; this one starts as soon as one is free."
            )
        await self._slots.acquire()
        driver = self._driver_factory(lambda tool, data, call_id: self._ask(session, tool, data, call_id))
        try:
            await driver.start(session.conv.session_id)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("the agent did not start")
            self._slots.release()
            session.last_problem = f"the agent did not start: {type(exc).__name__}"
            await self.chat.post(session.place, self._scrub(f"The agent did not start: {type(exc).__name__}: {exc}"))
            return False
        session.driver = driver
        self._live += 1
        return True

    async def _park(self, session: Session) -> None:
        driver, session.driver = session.driver, None
        if driver is None:
            return
        session.conv.session_id = driver.session_id or session.conv.session_id
        await driver.stop()
        self._live -= 1
        self._slots.release()
        self._save()

    async def _turn(self, session: Session, message: str) -> None:
        session.busy = True
        try:
            assert session.driver is not None  # noqa: S101 - _ensure_driver ran
            async for event in session.driver.turn(message):
                await self._on_event(session, event)
        finally:
            session.busy = False
            self._save()

    async def _on_event(self, session: Session, event: object) -> None:
        if isinstance(event, Text):
            session.last_text = await self._say(session.place, event.text)
        elif isinstance(event, Notice):
            session.last_problem = event.text
            await self._say(session.place, event.text)
        elif isinstance(event, ToolCall):
            if len(session.calls) >= MAX_TRACKED_CALLS:
                session.calls.pop(next(iter(session.calls)))
            session.calls[event.tool_use_id] = event
        elif isinstance(event, ToolOutput):
            await self._on_tool_output(session, event)
        elif isinstance(event, TurnEnd):
            session.conv.session_id = event.session_id or session.conv.session_id
            if event.error:
                session.last_problem = event.error
                await self._say(session.place, f"The agent stopped: {event.error}")

    async def _on_tool_output(self, session: Session, event: ToolOutput) -> None:
        call = session.calls.pop(event.tool_use_id, None)
        if call is None or call.name != "Bash" or event.is_error:
            return
        kind = harness.incident_call(str(call.input.get("command", "")))
        if kind in ("open", "attach"):
            state = harness.opened_incident(event.text)
            if state:
                await self._incident_opened(session, state)
        elif kind == "close" and session.conv.incident:
            await self._announce(f"{self._label(session)} {session.conv.incident} resolved: {session.conv.title}")

    async def _ask(self, session: Session, tool: str, tool_input: dict[str, Any], tool_use_id: str) -> PermissionAnswer:
        link = None
        if session.last_text is not None:
            with contextlib.suppress(Exception):
                link = await self.chat.permalink(session.last_text)
        return await self.broker.ask(
            incident=session.conv.incident,
            place=session.place,
            tool=tool,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            statement_link=link,
        )

    # --- incidents -----------------------------------------------------------------

    @staticmethod
    def _label(session: Session) -> str:
        return "Drill" if session.conv.drill else "Incident"

    async def _incident_opened(self, session: Session, state: dict[str, Any]) -> None:
        conv = session.conv
        if conv.incident == state["id"]:
            return
        conv.incident, conv.title, conv.drill = state["id"], state.get("title", ""), bool(state.get("drill"))
        self._save()
        if conv.thread is None:
            # Already in a channel of its own: attaching to another incident changes only the record.
            await self.chat.post(session.place, f"This channel now follows {conv.incident}: {state.get('url')}")
            return
        channel = await self._create_channel(session, state)
        if channel is None:
            return
        hub_thread = session.place
        try:
            await self.chat.invite(channel, sorted(self.settings.responders))
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not invite the responders")
            session.last_problem = "inviting the responders failed"
        conv.channel, conv.thread = channel, None
        del self.sessions[conv.key]
        conv.key = f"{channel}:None"
        self.sessions[conv.key] = session
        self._save()
        await self.chat.post(
            session.place,
            self._scrub(
                f"{self._label(session)} {conv.incident}: {conv.title}\nTracking issue: {state.get('url')}\n"
                "Messages from responders here reach the agent and are copied to the issue. "
                "Say `status` for the gateway's view."
            ),
        )
        link = self.chat.channel_link(channel)
        await self.chat.post(hub_thread, f"Continuing in {link}.", raw=True)
        # The title came from the agent: scrubbed and escaped before it sits beside the raw channel link.
        safe = [self.chat.escape(self._scrub(str(part))) for part in (conv.incident, conv.title, state.get("url"))]
        await self._announce(f"{self._label(session)} {safe[0]} opened: {safe[1]} · {safe[2]} · {link}", raw=True)

    async def _create_channel(self, session: Session, state: dict[str, Any]) -> str | None:
        for suffix in range(6):
            try:
                return await self.chat.create_private_channel(channel_name(state, suffix))
            except NameTaken:
                continue
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("could not create the incident channel")
                session.last_problem = f"creating the channel failed: {type(exc).__name__}"
                await self.chat.post(
                    session.place, "Could not create the incident channel; carrying on in this thread."
                )
                return None
        await self.chat.post(session.place, "Every channel name for this incident is taken; carrying on here.")
        return None

    async def _announce(self, text: str, raw: bool = False) -> None:
        """A one-line post in the hub. A raw text must already be scrubbed and escaped."""
        with contextlib.suppress(Exception):
            await self.chat.post(Place(self.settings.hub), text if raw else self._scrub(text), raw=raw)

    async def _archive_loop(self) -> None:
        while True:
            await asyncio.sleep(ARCHIVE_CHECK_SECONDS)
            await self.archive_closed()

    async def archive_closed(self) -> None:
        """Archive the channel of each incident whose tracking issue a person has closed."""
        for session in list(self.sessions.values()):
            conv = session.conv
            if conv.archived or conv.thread is not None or not conv.incident or session.busy:
                continue
            try:
                if not await self.tracker.is_closed(conv.incident):
                    continue
                await self.chat.post(session.place, "The tracking issue is closed; archiving this channel.")
                await self.chat.archive(conv.channel)
            except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("archive check failed for %s", conv.incident)
                continue
            conv.archived = True
            await session.queue.put(None)
            self._save()
            await self._announce(f"{self._label(session)} {conv.incident} closed; its channel is archived.")

    # --- helpers -------------------------------------------------------------------

    async def _say(self, place: Place, text: str) -> Posted | None:
        posted = None
        for part in chunks(self._scrub(text)):
            try:
                posted = await self.chat.post(place, part)
            except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("could not post to chat")
        return posted

    async def _name(self, user: str) -> str:
        try:
            return await self.chat.user_name(user)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            return user
