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
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sre_gateway import attachments, harness, prompts, questions
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
from sre_gateway.approvals import ApprovalBroker, Decision, Recorder
from sre_gateway.attachments import Limits
from sre_gateway.chat import ChatAdapter, Click, Command, Incoming, NameTaken, Place, Posted
from sre_gateway.indicator import THINKING, IndicatedChat, Indicator, activity_for
from sre_gateway.runner import Alert, Runner
from sre_gateway.store import Conversation, Store
from sre_gateway.tracker import Tracker

log = logging.getLogger(__name__)

CHUNK = 3500
ARCHIVE_CHECK_SECONDS = 3600
ARCHIVE_FIRST_SECONDS = 60
START_ATTEMPTS = 3
START_RETRY_SECONDS = 30
MAX_TRACKED_CALLS = 200
STATUS_WORDS = frozenset({"status", "!status"})
TRIAGE, CHAT = "triage-only", "chat"
RUNNER_NAME = "the alert source"
DRILL_ALERT = re.compile(r"^alert\s+(\S+)\s*(.*)$", re.IGNORECASE | re.DOTALL)
QUESTION = "question"
QUESTION_TURNS = 10
ENDED_QUESTIONS_KEPT = 200
QUESTION_REFUSAL = (
    "Refused without asking: this is a read-only question, which may only read. "
    "Do not retry; say in your answer what this would have checked or changed."
)
QUESTION_BUSY = "The agent sessions are busy with incident work; ask again later."
QUESTION_PREEMPTED = "This question ended to free the agent for incident work. Ask again with `/sre`."
QUESTION_ENDED = "This question has ended; ask again with `/sre`."
RECLAIMED = "The agent stopped to free a session for other work. Your next message here resumes it."
TRIAGE_REFUSAL = (
    "Refused without asking: this is an unattended triage, which may only read, keep its record and propose a fix. "
    "Do not retry; say in the summary what this would have checked or changed."
)

PolicyChange = harness.audit.PolicyChange
PolicyRecorder = Callable[[PolicyChange], None]


class DriverFactory(Protocol):  # pylint: disable=too-few-public-methods
    """(permission callback, triage token or None, whether it is a question) -> a driver under that policy."""

    def __call__(self, ask: AskPermission, triage: str | None, question: bool = False) -> AgentDriver: ...


@dataclass(frozen=True)
class Settings:
    hub: str
    responders: frozenset[str]
    approvers: frozenset[str]
    approval_ttl: int
    idle_seconds: int
    max_sessions: int
    state_dir: Path
    question_idle_seconds: int = 300
    attachments: Limits = Limits()


@dataclass
class Session:  # pylint: disable=too-many-instance-attributes
    conv: Conversation
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    driver: AgentDriver | None = None
    busy: bool = False
    last_text: Posted | None = None
    last_problem: str | None = None
    calls: dict[str, ToolCall] = field(default_factory=dict)
    worker: asyncio.Task | None = None
    release_after_turn: bool = False
    # Messages kept while the agent could not start; they go to it ahead of the next one.
    held: list[str] = field(default_factory=list)
    # The working and typing indicators, from the agent's start to the end of its turn.
    indicator: Indicator | None = None
    # A question's turns so far, and its log; an incident has neither.
    turns: int = 0
    # When the agent last finished a turn or started: the longest idle is reclaimed first.
    idle_since: float = 0.0
    qlog: questions.QuestionLog | None = None

    @property
    def question(self) -> bool:
        return self.conv.policy == QUESTION

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
    # Drop '[DRILL]' and a leading 'Drill:' so the prefix is not repeated in the name.
    title = re.sub(r"^(?:\[drill\]\s*)?(?:drill\b[\s:-]*)?", "", str(incident.get("title", "")), flags=re.IGNORECASE)
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
        driver_factory: DriverFactory,
        record: Recorder,
        scrub: Callable[[str], str],
        runner: Runner | None = None,
        record_policy: PolicyRecorder | None = None,
        env_values: dict[str, str] | None = None,
        audit_closed: Callable[[str], bool] | None = None,
    ):
        self.settings = settings
        # Every post goes through the indicators, so the working line stays last and typing clears first.
        self.chat = IndicatedChat(chat)
        self.tracker = tracker
        self.runner = runner
        self._driver_factory = driver_factory
        self._record_decision = record
        self._record_policy = record_policy
        self._scrub = scrub
        self._poller: asyncio.Task | None = None
        self._source_failing = False
        self.broker = ApprovalBroker(
            self.chat,
            approvers=settings.approvers,
            ttl_seconds=settings.approval_ttl,
            record=record,
            scrub=scrub,
            closed=audit_closed or (lambda _incident: False),
        )
        self.store = Store(settings.state_dir / "conversations.json")
        self.sessions: dict[str, Session] = {}
        self._slots = asyncio.Semaphore(settings.max_sessions)
        self._live = 0
        self._archiver: asyncio.Task | None = None
        # Sessions waiting for a free agent slot, and the threads of questions that have ended.
        self._waiting = 0
        self._ended_questions: dict[str, None] = {}
        self._env_values = env_values or {}

    # --- lifecycle -------------------------------------------------------------

    def load(self) -> None:
        for conv in self.store.load():
            if not conv.archived:
                self._add(conv)

    async def run(self) -> None:
        self.load()
        self._archiver = asyncio.create_task(self._archive_loop())
        if self.runner is not None and self.runner.settings.enabled:
            self._poller = asyncio.create_task(self._poll_loop())
        try:
            await self.chat.start(self)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for task in (self._archiver, self._poller):
            if task:
                task.cancel()
        for session in list(self.sessions.values()):
            if session.worker:
                session.worker.cancel()
            await self._park(session)
            if session.question:
                self._log_question(session, "ended", "the gateway stopped")
        self._save()

    def _add(self, conv: Conversation) -> Session:
        session = Session(conv)
        self.sessions[conv.key] = session
        session.worker = asyncio.create_task(self._work(session))
        return session

    def _save(self) -> None:
        # A question is never kept: it does not survive a restart, so it never resumes.
        self.store.save([s.conv for s in self.sessions.values() if not s.question])

    # --- chat events -------------------------------------------------------------

    async def on_command(self, command: Command) -> None:
        if command.name == "sre":
            await self._start_question(command)
            return
        if command.name != "incident":
            return
        if command.user not in self.settings.responders:
            await self.chat.tell(Place(command.channel), command.user, "Only listed responders can start an incident.")
            return
        text, drill = self._drill(command.text)
        if not text:
            await self.chat.tell(Place(command.channel), command.user, "Usage: /incident [drill] <what is broken>")
            return
        alert = DRILL_ALERT.match(text) if drill else None
        if alert:
            if self.runner is None:
                await self.chat.tell(Place(command.channel), command.user, "This site has no runner configured.")
                return
            # A drill of the alert path: the made-up alert starts a triage-only session, as a real one would.
            await self._start_triage([self.runner.drill(alert.group(1), alert.group(2).strip())], drill=True)
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
            reads = await self._read_files(message, session.place)
            await self._converse(session, message, reads)
        elif message.thread is not None and f"{message.channel}:{message.thread}" in self._ended_questions:
            await self.chat.tell(Place(message.channel, message.thread), message.user, QUESTION_ENDED)
        elif message.channel == self.settings.hub and message.thread is None and message.mentions_bot:
            text, drill = self._drill(message.text)
            if text or message.files:
                place = Place(self.settings.hub, message.ts)
                reads = await self._read_files(message, place)
                name = await self._name(message.user)
                await self._start(message.user, name, self._with_files(text, reads), place, drill)

    async def _read_files(self, message: Incoming, place: Place) -> list[attachments.Read]:
        """Read a message's text attachments (ADR 0023); say to its sender, and only them, what was not read."""
        if not message.files:
            return []
        reads = await attachments.read_all(message.files, self.settings.attachments, self.chat.download, self._scrub)
        notice = attachments.skipped_notice(reads)
        if notice:
            with contextlib.suppress(Exception):
                await self.chat.tell(place, message.user, notice)
        return reads

    @staticmethod
    def _with_files(text: str, reads: list[attachments.Read]) -> str:
        """A message's text followed by its attachments as the agent should see them."""
        if not reads:
            return text
        return f"{text or '(no text)'}\n\n{attachments.prompt_block(reads)}"

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

    async def _converse(self, session: Session, message: Incoming, reads: list[attachments.Read] | None = None) -> None:
        reads = reads or []
        text = message.text.strip()
        if not reads and text.lower() in STATUS_WORDS:
            await self.chat.post(session.place, self.status(session))
            return
        name = await self._name(message.user)
        if session.conv.incident:
            await self._mirror(session, message, name, reads)
        heard = self._with_files(text, reads)
        if session.conv.policy == TRIAGE:
            await self._end_triage(session, message, name)
            await session.queue.put(prompts.joined_message(name, heard))
            return
        if session.question:
            logged = "\n".join([text, *attachments.metadata_lines(reads)])
            self._log_question(session, "message", message.user, logged)
        if session.busy:
            await self.chat.tell(session.place, message.user, "Queued: the agent is still on the previous message.")
        await session.queue.put(f"[chat] {name}: {heard}")

    # --- questions -----------------------------------------------------------------

    async def _start_question(self, command: Command) -> None:
        """'/sre <question>': a read-only session in a hub thread that opens no incident (ADR 0022)."""
        where = Place(command.channel)
        if command.user not in self.settings.responders:
            await self.chat.tell(where, command.user, "Only listed responders can ask the agent.")
            return
        text = command.text.strip()
        if not text:
            await self.chat.tell(where, command.user, "Usage: /sre <question>")
            return
        if not self._question_room():
            await self.chat.tell(where, command.user, QUESTION_BUSY)
            return
        qlog = questions.QuestionLog(self.settings.state_dir / "questions", questions.new_id(), self._env_values)
        try:
            qlog.asked(command.user, text)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            # A question that left no record is not asked.
            log.exception("could not write the question log")
            await self.chat.tell(where, command.user, "The question could not be recorded, so it was not asked.")
            return
        name = await self._name(command.user)
        posted = await self.chat.post(Place(self.settings.hub), self._scrub(f"{name} asked: {text}"))
        place = Place(self.settings.hub, posted.ts)
        conv = Conversation(
            key=f"{place.channel}:{place.thread}",
            channel=place.channel,
            thread=place.thread,
            started_by=command.user,
            policy=QUESTION,
        )
        session = self._add(conv)
        session.qlog = qlog
        await session.queue.put(prompts.question_message(name, text))

    def _question_room(self) -> bool:
        """Whether a question may take an agent session: only if one is still left for incident work.

        An ended incident's idle agent does not count: a question may reclaim it.
        """
        busy = self._live - len(self._reclaimable(for_question=True))
        return busy + self._waiting + 1 < self.settings.max_sessions

    def _reclaimable(self, for_question: bool) -> list[Session]:
        """Idle agents that hold a slot without doing work, in the order they are reclaimed.

        Ended incidents first (their work is done), then, unless a question is asking, idle
        questions (a person may still be asking); the longest idle first within each.
        """
        idle = [s for s in self.sessions.values() if s.driver is not None and not s.busy]
        ended = [s for s in idle if s.conv.ended and not s.question]
        asked = [] if for_question else [s for s in idle if s.question]
        return sorted(ended, key=lambda s: s.idle_since) + sorted(asked, key=lambda s: s.idle_since)

    async def _reclaim(self, for_question: bool) -> bool:
        """Stop one reclaimable agent so other work can take its slot. False if there was none."""
        found = self._reclaimable(for_question)
        if not found:
            return False
        await self._release(found[0])
        return True

    async def _release(self, session: Session) -> None:
        """Free a session's slot for waiting work: a question ends; an ended incident is parked, to resume later."""
        if session.question:
            await self._end_question(session, QUESTION_PREEMPTED, "preempted")
            return
        await self._park(session)
        await self._say(session.place, RECLAIMED)

    async def _after_question_turn(self, session: Session) -> None:
        if session.conv.ended:
            return
        session.turns += 1
        if session.turns >= QUESTION_TURNS:
            await self._end_question(
                session, f"This question reached its limit of {QUESTION_TURNS} turns. Ask again with `/sre`.", "turns"
            )

    async def _end_question(self, session: Session, notice: str | None, reason: str) -> None:
        """Stop a question for good: its slot is freed, its thread answers only with a pointer to /sre."""
        conv = session.conv
        if conv.ended:
            return
        conv.ended = conv.archived = True
        await self._park(session)
        self.sessions.pop(conv.key, None)
        self._ended_questions[conv.key] = None
        while len(self._ended_questions) > ENDED_QUESTIONS_KEPT:
            self._ended_questions.pop(next(iter(self._ended_questions)))
        self._log_question(session, "ended", reason)
        if notice:
            await self._say(session.place, notice)
        # The worker takes this as its signal to stop.
        await session.queue.put(None)

    def _log_question(self, session: Session, event: str, *args: Any) -> None:
        """Write one record to a question's log. A failure is reported in status, never raised."""
        if session.qlog is None:
            return
        try:
            getattr(session.qlog, event)(*args)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not write the question log")
            session.last_problem = f"writing the question log failed: {type(exc).__name__}"

    # --- unattended triage ---------------------------------------------------------

    def _open_sessions(self, drill: bool) -> list[Session]:
        """Conversations whose incident is still being worked, real or drill.

        One the agent has closed or dismissed is not, even while its channel waits for the archive sweep.
        """
        return [
            s
            for s in self.sessions.values()
            if not s.question and not s.conv.archived and not s.conv.ended and s.conv.drill == drill
        ]

    async def on_alerts(self, alerts: list[Alert]) -> None:
        """New qualifying problems from one poll: into an open incident, else one new triage, else a notice."""
        if not alerts or self.runner is None:
            return
        lines = [alert.line() for alert in alerts]
        open_now = self._open_sessions(drill=False)
        if open_now:
            # The newest open incident hears about them; whether they belong to it is the agent's (and people's) call.
            session = open_now[-1]
            await self._say(session.place, "New alerts while this incident is open:\n" + "\n".join(lines))
            await session.queue.put(prompts.alert_in_incident(lines))
        else:
            # Only one unattended triage is ever being worked: while one is open, alerts join it (above).
            # Drills and incidents already closed or dismissed take no part in that choice.
            await self._start_triage(alerts, drill=False)
        self.runner.mark(alerts)

    async def _start_triage(self, alerts: list[Alert], drill: bool) -> None:
        lines = [alert.line() for alert in alerts]
        kind = "Drill: unattended triage" if drill else "Unattended triage"
        posted = await self.chat.post(
            Place(self.settings.hub), self._scrub(f"{kind} started for:\n" + "\n".join(lines))
        )
        place = Place(self.settings.hub, posted.ts)
        conv = Conversation(
            key=f"{place.channel}:{place.thread}",
            channel=place.channel,
            thread=place.thread,
            drill=drill,
            started_by=RUNNER_NAME,
            auto=True,
            policy=TRIAGE,
            triage_token=secrets.token_hex(16),
        )
        session = self._add(conv)
        self._save()
        await session.queue.put(prompts.alert_message(lines, drill))

    async def _end_triage(self, session: Session, message: Incoming, name: str) -> None:
        """A responder has spoken: the session leaves triage-only, once and for good, on the record."""
        conv = session.conv
        link = None
        with contextlib.suppress(Exception):
            link = await self.chat.permalink(Posted(message.channel, message.ts))
        if conv.incident and self._record_policy is not None:
            try:
                self._record_policy(
                    PolicyChange(conv.incident, conv.session_id, TRIAGE, CHAT, f"{name} ({message.user})", link)
                )
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("could not record the policy change")
                session.last_problem = f"recording the policy change failed: {type(exc).__name__}"
        conv.policy = CHAT
        self._save()
        # The running agent was started with the triage-only rules; the next turn starts it again with the chat rules.
        if session.busy:
            session.release_after_turn = True
        else:
            await self._park(session)
        await self.chat.post(
            session.place,
            f"{name} has joined: this session is no longer triage-only. "
            "Commands outside the allow-list now raise approval requests.",
        )

    async def _poll_loop(self) -> None:
        assert self.runner is not None  # noqa: S101 - started only with a runner
        while True:
            await self.poll_once()
            await asyncio.sleep(self.runner.settings.poll_seconds)

    async def poll_once(self) -> None:
        assert self.runner is not None  # noqa: S101 - called only with a runner
        try:
            selection = await self.runner.poll(starting=not self._open_sessions(drill=False))
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("polling the alert source failed")
            if not self._source_failing:
                self._source_failing = True
                await self._announce(f"The alert source could not be read ({type(exc).__name__}); triage is paused.")
            return
        if self._source_failing:
            self._source_failing = False
            await self._announce("The alert source is readable again; triage resumes.")
        await self.on_alerts(selection.start)

    async def _mirror(
        self, session: Session, message: Incoming, name: str, reads: list[attachments.Read] | None = None
    ) -> None:
        """Copy a person's message to the tracking issue, so the issue stays the record.

        An attachment is described, never copied: a log can hold a secret the redaction misses, and the issue is
        not the place for it. The link leads to the original in chat (ADR 0023).
        """
        link = None
        with contextlib.suppress(Exception):
            link = await self.chat.permalink(Posted(message.channel, message.ts))
        body = f"**{name}** (chat): {self._scrub(message.text.strip()) or ('(no text)' if reads else '')}"
        for line in attachments.metadata_lines(reads or []):
            body += f"\n\n{line}"
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
        first = (
            f"Question: read-only, no incident; turns used {session.turns} of {QUESTION_TURNS}"
            if session.question
            else f"Incident: {conv.incident or 'not opened yet'}{' (drill)' if conv.drill else ''}"
        )
        lines = [
            first,
            f"Started by: {'the alert source' if conv.auto else 'a responder'}; policy: {conv.policy}",
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
        idle = self.settings.question_idle_seconds if session.question else self.settings.idle_seconds
        while True:
            try:
                if session.driver is not None:
                    message = await asyncio.wait_for(session.queue.get(), idle)
                else:
                    message = await session.queue.get()
            except TimeoutError:
                if session.question:
                    minutes = max(1, idle // 60)
                    await self._end_question(
                        session,
                        f"This question ended after {minutes} minute(s) with no message. Ask again with `/sre`.",
                        "idle",
                    )
                    continue
                await self._park(session)
                continue
            if message is None:
                await self._park(session)
                return
            session.indicator = await self.chat.open(lambda: session.place)
            try:
                if not await self._begin(session, message):
                    continue
                if session.held:
                    message = "\n\n".join([*session.held, message])
                    session.held.clear()
                session.indicator.set_activity(THINKING)
                await self._turn(session, message)
            finally:
                indicator, session.indicator = session.indicator, None
                await self.chat.close(indicator)
            if session.question:
                await self._after_question_turn(session)
            if self._waiting and session.driver is not None and (session.question or session.conv.ended):
                # Work is waiting for a slot this session holds without needing it.
                await self._release(session)
            if session.release_after_turn:
                session.release_after_turn = False
                await self._park(session)

    async def _begin(self, session: Session, message: str) -> bool:
        """Have an agent ready for this message; False if there is none, after dealing with the message."""
        if session.question:
            if await self._ensure_driver(session):
                return True
            # One try, no waiting: a question that cannot start now is simply asked again later.
            await self._end_question(session, None, "did not start")
            return False
        if await self._start_with_retries(session):
            return True
        # Kept, not dropped: an alert's first message is the only time the agent hears of it.
        session.held.append(message)
        return False

    async def _start_with_retries(self, session: Session) -> bool:
        """Start the agent, trying again after a pause: a start can fail on a passing network or DNS fault."""
        for attempt in range(1, START_ATTEMPTS + 1):
            if await self._ensure_driver(session):
                return True
            if attempt < START_ATTEMPTS:
                wait = START_RETRY_SECONDS * attempt
                await self.chat.post(session.place, f"Trying again in {wait} seconds ({attempt} of {START_ATTEMPTS}).")
                await asyncio.sleep(wait)
        await self.chat.post(
            session.place,
            f"The agent did not start after {START_ATTEMPTS} tries. Its message is kept: "
            "send any message here and it goes to the agent first.",
        )
        return False

    async def _ensure_driver(self, session: Session) -> bool:
        if session.driver is not None:
            return True
        if session.conv.ended and session.question:
            return False
        if session.question:
            # A question never waits: a slot left free, or one an ended incident holds idle, or none.
            if self._question_room() and self._slots.locked():
                await self._reclaim(for_question=True)
            if not self._question_room() or self._slots.locked():
                await self.chat.post(session.place, QUESTION_BUSY)
                return False
        elif self._slots.locked():
            # Incident work never waits behind an agent that is only sitting idle.
            await self._reclaim(for_question=False)
            if self._slots.locked():
                await self.chat.post(
                    session.place, "All agent sessions are in use; this one starts as soon as one is free."
                )
        self._waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1
        driver = self._driver_factory(
            lambda tool, data, call_id: self._ask(session, tool, data, call_id),
            session.conv.triage_token if session.conv.policy == TRIAGE else None,
            question=session.question,
        )
        try:
            await driver.start(session.conv.session_id)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("the agent did not start")
            self._slots.release()
            session.last_problem = f"the agent did not start: {type(exc).__name__}"
            await self.chat.post(session.place, self._scrub(f"The agent did not start: {type(exc).__name__}: {exc}"))
            return False
        session.driver = driver
        session.idle_since = time.monotonic()
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
            session.idle_since = time.monotonic()
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
            if session.question:
                self._log_question(session, "attempt", event.name, event.input, event.tool_use_id)
            if session.indicator is not None:
                session.indicator.set_activity(activity_for(event.name, event.input))
        elif isinstance(event, ToolOutput):
            if session.indicator is not None:
                session.indicator.set_activity(THINKING)
            if session.question:
                # A question has no record to follow: its output is only logged, never read for incident calls.
                session.calls.pop(event.tool_use_id, None)
                self._log_question(session, "complete", event.tool_use_id, event.text, event.is_error)
                return
            await self._on_tool_output(session, event)
        elif isinstance(event, TurnEnd):
            session.conv.session_id = event.session_id or session.conv.session_id
            if event.error:
                session.last_problem = event.error
                await self._say(session.place, f"The agent stopped: {event.error}")
                if not session.conv.incident and not session.question:
                    # Nothing to keep warm: free the slot for others; the next message resumes the session.
                    session.release_after_turn = True

    async def _on_tool_output(self, session: Session, event: ToolOutput) -> None:
        call = session.calls.pop(event.tool_use_id, None)
        if call is None or call.name != "Bash" or event.is_error:
            return
        kind = harness.incident_call(str(call.input.get("command", "")))
        if kind in ("open", "attach", "promote"):
            state = harness.opened_incident(event.text)
            if state:
                before = session.conv.incident
                await self._incident_opened(session, state)
                if kind == "promote" and before:
                    await self._announce(
                        f"Unattended triage {before} promoted to incident {state['id']}: {state.get('url')}"
                    )
        elif kind == "close" and session.conv.incident:
            self._end(session)
            await self._announce(f"{self._label(session)} {session.conv.incident} resolved: {session.conv.title}")
        elif kind == "dismiss" and session.conv.incident:
            self._end(session)
            await self._announce(f"Unattended triage {session.conv.incident} dismissed: not an incident.")

    def _end(self, session: Session) -> None:
        """The incident is resolved or dismissed: it takes no more alerts and no triage slot."""
        session.conv.ended = True
        self._save()

    async def _ask(self, session: Session, tool: str, tool_input: dict[str, Any], tool_use_id: str) -> PermissionAnswer:
        if session.conv.policy == TRIAGE:
            return self._refuse_in_triage(session, tool, tool_input, tool_use_id)
        if session.question:
            # No approval request for a question: it is read-only for life (ADR 0022).
            self._log_question(session, "refused", tool, tool_input, tool_use_id)
            return PermissionAnswer(False, QUESTION_REFUSAL)
        link = None
        if session.last_text is not None:
            with contextlib.suppress(Exception):
                link = await self.chat.permalink(session.last_text)
        indicator = session.indicator
        if indicator is not None:
            await indicator.waiting()
        try:
            return await self.broker.ask(
                incident=session.conv.incident,
                place=session.place,
                tool=tool,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                statement_link=link,
            )
        finally:
            if indicator is not None:
                await indicator.resumed()

    def _refuse_in_triage(
        self, session: Session, tool: str, tool_input: dict[str, Any], tool_use_id: str
    ) -> PermissionAnswer:
        """No approval request in an unattended triage: the call is refused, and the refusal is on the record."""
        if session.conv.incident:
            try:
                self._record_decision(
                    Decision(
                        incident=session.conv.incident,
                        outcome="refused",
                        tool=tool,
                        tool_input=tool_input,
                        tool_use_id=tool_use_id,
                        approver=None,
                        link=None,
                        reason="triage-only session",
                    )
                )
            except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("could not record a triage refusal")
                session.last_problem = f"recording a refusal failed: {type(exc).__name__}"
        return PermissionAnswer(False, TRIAGE_REFUSAL)

    # --- incidents -----------------------------------------------------------------

    @staticmethod
    def _label(session: Session) -> str:
        return "Drill" if session.conv.drill else "Incident"

    async def _incident_opened(self, session: Session, state: dict[str, Any]) -> None:
        conv = session.conv
        if conv.ended and state.get("status") != "resolved":
            # Working an open incident again: it takes alerts and is no longer reclaimable.
            conv.ended = False
            self._save()
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
        # Soon after start, not an hour after: an incident closed while the gateway was down still holds
        # its conversation open until the first sweep.
        await asyncio.sleep(ARCHIVE_FIRST_SECONDS)
        while True:
            await self.archive_closed()
            self.prune_questions()
            await asyncio.sleep(ARCHIVE_CHECK_SECONDS)

    def prune_questions(self) -> None:
        """Remove question logs past their retention."""
        try:
            questions.prune(self.settings.state_dir / "questions")
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not prune the question logs")

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
