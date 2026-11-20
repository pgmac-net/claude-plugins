"""Feedback in chat while an agent works: the working indicator and the typing indicator.

The working indicator is one line the gateway posts, e.g.
'sre-harness is working: running kube_read.py (1m 20s)'. It is kept as the
last message in the conversation, edited as the agent moves between tools, and
deleted when the turn ends. Its activity is a fixed label from the tool's name,
never the tool's input.

The typing indicator is the platform's own 'sre-harness is writing…' status,
where the platform has one. It is on while the agent works, off just before
every post into the conversation and back on after it, and off while an
approval request waits for a person.

Both are best-effort: a platform error is logged and dropped, and never fails a
turn or holds back a post.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from sre_gateway.chat import ApprovalView, ChatAdapter, Place, Posted

log = logging.getLogger(__name__)

NAME = "sre-harness"
TICK_SECONDS = 5
HEARTBEAT_SECONDS = 30
TYPING_REFRESH_SECONDS = 90  # Slack drops the status after two minutes without a reply.

STARTING, THINKING, WAITING = "starting", "thinking", "waiting for approval"
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
# Named in the line only when a command runs one of these, matched on the script's file name.
HARNESS_SCRIPTS = frozenset(
    {
        "alerts.py",
        "assist.py",
        "audit.py",
        "autonomy.py",
        "budget.py",
        "catalog_check.py",
        "catalog_sync.py",
        "incident.py",
        "kube_read.py",
        "report.py",
        "slo.py",
        "slo_dashboard.py",
        "state_gc.py",
        "stpa.py",
        "timeline.py",
        "trends.py",
        "validate_site.py",
    }
)
SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def activity_for(tool: str, tool_input: dict[str, Any]) -> str:
    """What the line says the agent is doing. Built from fixed words: no part of the input is shown."""
    if tool in READ_TOOLS:
        return "reading files"
    if tool == "Skill":
        return "using a skill"
    if tool == "Bash":
        for word in str(tool_input.get("command", "")).split():
            script = word.strip("'\"").rsplit("/", 1)[-1]
            if script in HARNESS_SCRIPTS:
                return f"running {script}"
        return "running a shell command"
    if tool.startswith("mcp__"):
        parts = [part for part in tool.split("__")[1:] if SAFE_TOOL_NAME.fullmatch(part)]
        return f"using {' '.join(parts)}" if parts else "using a tool"
    return f"using {tool}" if SAFE_TOOL_NAME.fullmatch(tool) else "using a tool"


def elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def line(activity: str, seconds: float) -> str:
    if activity == STARTING:
        return f"{NAME} is starting… ({elapsed(seconds)})"
    return f"{NAME} is working: {activity}… ({elapsed(seconds)})"


class Indicator:  # pylint: disable=too-many-instance-attributes
    """Both indicators for one conversation, from the agent's start to the end of its turn."""

    def __init__(self, chat: ChatAdapter, where: Callable[[], Place], clock: Callable[[], float] = time.monotonic):
        self._chat = chat
        self._where = where
        self._clock = clock
        self._lock = asyncio.Lock()
        self._started = clock()
        self._activity = STARTING
        self._posted: Posted | None = None
        self._shown = ""
        self._last_beat = self._started
        self._typing_place: Place | None = None
        self._typing_since = 0.0
        self._waiting = False
        self._before_wait = THINKING
        self._task: asyncio.Task | None = None
        self.closed = False

    @property
    def place(self) -> Place:
        return self._where()

    async def open(self) -> None:
        async with self._lock:
            await self._repost()
            await self._typing(True)
        self._task = asyncio.create_task(self._tick())

    def set_activity(self, activity: str) -> None:
        """Change the label; the next tick shows it, so a burst of tool calls is at most one edit per tick."""
        self._activity = activity

    async def waiting(self) -> None:
        """An approval request is about to be posted: the agent is waiting on a person, not writing."""
        if not self._waiting:
            self._before_wait = self._activity
        self._waiting = True
        self._activity = WAITING
        async with self._lock:
            await self._typing(False)
            await self._edit()

    async def resumed(self) -> None:
        """The request was answered: back to what the agent was doing (an approved tool runs next)."""
        self._waiting = False
        self._activity = self._before_wait
        async with self._lock:
            await self._edit()
            await self._typing(True)

    async def before_post(self) -> None:
        async with self._lock:
            await self._typing(False)

    async def after_post(self) -> None:
        """Something was posted under the line: move the line below it, then show typing again."""
        async with self._lock:
            if self.closed:
                return
            await self._repost()
            if not self._waiting:
                await self._typing(True)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        async with self._lock:
            await self._typing(False)
            await self._remove()

    # --- internals: callers hold the lock -------------------------------------------

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            async with self._lock:
                if self.closed:
                    return
                now = self._clock()
                if self._activity != self._shown or now - self._last_beat >= HEARTBEAT_SECONDS:
                    await self._edit()
                if self._typing_place is not None and now - self._typing_since >= TYPING_REFRESH_SECONDS:
                    await self._typing(True)

    async def _edit(self) -> None:
        if self._posted is None:
            await self._repost()
            return
        text = line(self._activity, self._clock() - self._started)
        try:
            await self._chat.update(self._posted, text)
            self._shown, self._last_beat = self._activity, self._clock()
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning("could not update the working indicator", exc_info=True)

    async def _repost(self) -> None:
        await self._remove()
        text = line(self._activity, self._clock() - self._started)
        try:
            self._posted = await self._chat.post(self.place, text)
            self._shown, self._last_beat = self._activity, self._clock()
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning("could not post the working indicator", exc_info=True)

    async def _remove(self) -> None:
        posted, self._posted = self._posted, None
        if posted is None:
            return
        try:
            await self._chat.delete(posted)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning("could not delete the working indicator; marking it finished", exc_info=True)
            with contextlib.suppress(Exception):
                await self._chat.update(posted, f"{NAME} finished.")

    async def _typing(self, on: bool) -> None:
        place = self.place
        try:
            if self._typing_place is not None and (not on or self._typing_place != place):
                old, self._typing_place = self._typing_place, None
                await self._chat.set_typing(old, False)
            if on:
                self._typing_place, self._typing_since = place, self._clock()
                await self._chat.set_typing(place, True)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning("could not set the typing indicator", exc_info=True)


class IndicatedChat:
    """The gateway's chat: every post into a conversation that has an indicator open goes around it.

    Typing goes off before the post and the line moves below it after, so the line
    stays last and typing never shows over a message that has already arrived.
    """

    def __init__(self, chat: ChatAdapter, clock: Callable[[], float] = time.monotonic):
        self._chat = chat
        self._clock = clock
        self._open: list[Indicator] = []

    async def open(self, where: Callable[[], Place]) -> Indicator:
        """Start both indicators for a conversation; 'where' is read on every use, so they follow it if it moves."""
        indicator = Indicator(self._chat, where, self._clock)
        self._open.append(indicator)
        await indicator.open()
        return indicator

    async def close(self, indicator: Indicator) -> None:
        with contextlib.suppress(ValueError):
            self._open.remove(indicator)
        await indicator.close()

    def _at(self, place: Place) -> Indicator | None:
        for indicator in self._open:
            if not indicator.closed and indicator.place == place:
                return indicator
        return None

    async def post(self, place: Place, text: str, raw: bool = False) -> Posted:
        indicator = self._at(place)
        if indicator is None:
            return await self._chat.post(place, text, raw)
        await indicator.before_post()
        try:
            return await self._chat.post(place, text, raw)
        finally:
            await indicator.after_post()

    async def post_approval(self, place: Place, view: ApprovalView) -> Posted:
        indicator = self._at(place)
        if indicator is None:
            return await self._chat.post_approval(place, view)
        await indicator.before_post()
        try:
            return await self._chat.post_approval(place, view)
        finally:
            await indicator.after_post()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)
