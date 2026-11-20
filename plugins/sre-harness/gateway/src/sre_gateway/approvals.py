"""Approval requests: one per tool call, single-use, expiring, answered only by listed approvers.

The decision is written to the incident's audit log before the agent is
answered. If it cannot be written, the call is refused: an approval that
left no record must not let a command run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sre_gateway.agent import PermissionAnswer
from sre_gateway.chat import ApprovalView, ChatAdapter, Click, Place, Posted

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 1500
WHY_LIMIT = 300
CLOSED_REFUSAL = (
    "Refused: the audit log of {incident} is committed and closed, so no approval can be recorded. "
    "Do not retry; a person can run it from a terminal, or start a new incident with /incident."
)


@dataclass(frozen=True)
class Decision:
    """What the broker hands to the audit log."""

    incident: str
    outcome: str
    tool: str
    tool_input: dict[str, Any]
    tool_use_id: str
    approver: str | None
    link: str | None
    reason: str | None = None


Recorder = Callable[[Decision], None]


@dataclass
class _Pending:
    view: ApprovalView
    incident: str
    tool_input: dict[str, Any]
    tool_use_id: str
    place: Place
    posted: Posted | None = None
    link: str | None = None
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    closed: bool = False


def describe(tool: str, tool_input: dict[str, Any]) -> tuple[str, str | None]:
    """(what will run, the agent's own description of it) for a request. The caller redacts both.

    The description is the agent's words, not part of what runs, so it is shown apart and marked so.
    """
    if tool == "Bash":
        description = tool_input.get("description")
        return str(tool_input.get("command", "")), str(description) if description else None
    return json.dumps(tool_input, indent=1, sort_keys=True, default=str), None


class ApprovalBroker:
    def __init__(  # noqa: PLR0913  pylint: disable=too-many-arguments
        self,
        chat: ChatAdapter,
        *,
        approvers: frozenset[str],
        ttl_seconds: int,
        record: Recorder,
        scrub: Callable[[str], str],
        closed: Callable[[str], bool] = lambda _incident: False,
    ):
        self._closed = closed
        self._chat = chat
        self._approvers = approvers
        self._ttl = ttl_seconds
        self._record = record
        self._scrub = scrub
        self._pending: dict[str, _Pending] = {}

    @property
    def open_requests(self) -> int:
        return sum(1 for pending in self._pending.values() if not pending.closed)

    async def ask(  # noqa: PLR0913  pylint: disable=too-many-arguments
        self,
        *,
        incident: str | None,
        place: Place,
        tool: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        statement_link: str | None,
    ) -> PermissionAnswer:
        """Post a request and wait for an approver, or for it to expire."""
        if not incident:
            return PermissionAnswer(
                False,
                "Refused: open or attach the incident record first (incident.py open). "
                "Approvals are recorded in its audit log, so none can be given before it exists.",
            )
        if self._closed(incident):
            # Nothing can be written to a committed log, so no approval can count: say so before anyone clicks.
            return PermissionAnswer(False, CLOSED_REFUSAL.format(incident=incident))
        command, description = describe(tool, tool_input)
        summary = self._scrub(command)
        if len(summary) > SUMMARY_LIMIT:
            # An approver must see all of what would run; a cut-off request could hide the part that matters.
            return PermissionAnswer(
                False,
                f"Refused: the call is {len(summary)} characters, too long to show an approver in full "
                f"(limit {SUMMARY_LIMIT}). Split it, or put the steps in a script and run that.",
            )
        view = ApprovalView(
            request_id=secrets.token_hex(12),
            tool=tool,
            summary=summary,
            statement_link=statement_link,
            expires_in_seconds=self._ttl,
            description=self._scrub(description)[:300] if description else None,
        )
        pending = _Pending(view, incident, tool_input, tool_use_id, place)
        self._pending[view.request_id] = pending
        try:
            pending.posted = await self._chat.post_approval(place, view)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not post an approval request")
            pending.closed = True
            self._pending.pop(view.request_id, None)
            return PermissionAnswer(False, "Refused: the approval request could not be posted to chat.")
        try:
            pending.link = await self._chat.permalink(pending.posted)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not fetch the request's link")
        try:
            return await asyncio.wait_for(asyncio.shield(pending.done), self._ttl)
        except TimeoutError:
            return await self._expire(pending)
        finally:
            self._pending.pop(view.request_id, None)

    async def _expire(self, pending: _Pending) -> PermissionAnswer:
        if pending.closed:
            # A click won the race; its answer is already on the future.
            return await pending.done
        pending.closed = True
        answer = PermissionAnswer(False, "Refused: the approval request expired with no answer. Ask again in chat.")
        try:
            self._write(pending, "expired", None, None)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not record an expiry")
        await self._close(pending, "Expired with no answer. Nothing ran.")
        return answer

    async def on_click(self, click: Click) -> None:
        pending = self._pending.get(click.request_id)
        if pending is None or pending.closed:
            await self._tell(pending, click, "This request is no longer open.")
            return
        if click.user not in self._approvers:
            # The request stays open for a listed approver.
            await self._tell(pending, click, "Only the approvers listed in the site configuration can answer this.")
            return
        # Claimed before any await, so a second click or the expiry cannot also answer it.
        pending.closed = True
        outcome = "approved" if click.action == "approve" else "denied"
        name = await self._name(click.user)
        try:
            self._write(pending, outcome, click.user, click.reason)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not record a decision")
            why = self._why(exc)
            await self._close(pending, f"{name} answered, but the decision could not be recorded ({why}). Refused.")
            pending.done.set_result(PermissionAnswer(False, f"Refused: the approval could not be recorded: {why}"))
            return
        if outcome == "approved":
            await self._close(pending, f"Approved by {name}.")
            pending.done.set_result(PermissionAnswer(True))
        else:
            reason = f": {self._scrub(click.reason)[:500]}" if click.reason else ""
            await self._close(pending, f"Denied by {name}{reason}")
            pending.done.set_result(PermissionAnswer(False, f"Refused by {name}{reason}"))

    def _why(self, exc: Exception) -> str:
        """Why a decision could not be written, scrubbed and short enough for a card (homelabia#266)."""
        text = self._scrub(str(exc)).strip() or type(exc).__name__
        return text if len(text) <= WHY_LIMIT else text[: WHY_LIMIT - 1] + "…"

    def _write(self, pending: _Pending, outcome: str, approver: str | None, reason: str | None) -> None:
        link = pending.link
        if link is None and pending.posted is not None:
            link = f"{pending.posted.channel}/{pending.posted.ts}"
        self._record(
            Decision(
                incident=pending.incident,
                outcome=outcome,
                tool=pending.view.tool,
                tool_input=pending.tool_input,
                tool_use_id=pending.tool_use_id,
                approver=approver,
                link=link,
                reason=reason,
            )
        )

    async def _close(self, pending: _Pending, outcome: str) -> None:
        if pending.posted is None:
            return
        try:
            await self._chat.close_approval(pending.posted, pending.view, outcome)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not update an approval request")

    async def _tell(self, pending: _Pending | None, click: Click, text: str) -> None:
        if pending is None:
            log.info("click on unknown or finished request %s", click.request_id)
            return
        try:
            await self._chat.tell(pending.place, click.user, text)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("could not answer a click")

    async def _name(self, user: str) -> str:
        try:
            return await self._chat.user_name(user)
        except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
            return user
