"""The agent interface, and the driver that runs Claude Code through the Agent SDK.

The gateway sees an agent as something that takes a message and yields
events: text to post, tool calls and their output, and the end of a turn.
A permission prompt is a callback the driver makes into the gateway.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class ToolCall:
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolOutput:
    tool_use_id: str
    text: str
    is_error: bool


@dataclass(frozen=True)
class Notice:
    """Something the people in the channel should know that is not the agent's own words."""

    text: str


@dataclass(frozen=True)
class TurnEnd:
    session_id: str | None
    error: str | None = None
    cost_usd: float | None = None


Event = Text | ToolCall | ToolOutput | Notice | TurnEnd


@dataclass(frozen=True)
class PermissionAnswer:
    allow: bool
    message: str = ""


# (tool name, exact input, tool_use_id) -> answer
AskPermission = Callable[[str, dict[str, Any], str], Awaitable[PermissionAnswer]]


class AgentDriver(Protocol):
    session_id: str | None

    async def start(self, resume: str | None) -> None: ...

    def turn(self, message: str) -> AsyncIterator[Event]: ...

    async def stop(self) -> None: ...


DriverFactory = Callable[[AskPermission], AgentDriver]


def _tool_output_text(content: Any) -> str:
    """Tool output as text: the SDK gives a string or a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return "" if content is None else str(content)


class SdkDriver:
    """Runs one Claude Code session through the Claude Agent SDK."""

    def __init__(self, options_for: Callable[[str | None, Any], Any], ask: AskPermission):
        self._options_for = options_for
        self._ask = ask
        self._client: Any = None
        self.session_id: str | None = None

    async def _can_use_tool(self, tool: str, tool_input: dict[str, Any], context: Any) -> Any:
        # Imported here so tests that never start a real agent do not need the SDK's CLI.
        from claude_agent_sdk import (  # noqa: PLC0415  pylint: disable=import-outside-toplevel
            PermissionResultAllow,
            PermissionResultDeny,
        )

        tool_use_id = getattr(context, "tool_use_id", None) or ""
        if not tool_use_id:
            return PermissionResultDeny(message="Refused: the call has no id, so an approval could not be bound to it.")
        answer = await self._ask(tool, tool_input, tool_use_id)
        if answer.allow:
            # The input is passed back unchanged: the approval covered exactly this input.
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=answer.message or "Refused.")

    async def start(self, resume: str | None) -> None:
        from claude_agent_sdk import ClaudeSDKClient  # noqa: PLC0415  pylint: disable=import-outside-toplevel

        self._client = ClaudeSDKClient(options=self._options_for(resume, self._can_use_tool))
        await self._client.connect()
        self.session_id = resume

    async def turn(self, message: str) -> AsyncIterator[Event]:
        if self._client is None:
            raise RuntimeError("the agent session is not started")
        try:
            await self._client.query(message)
            async for item in self._client.receive_response():
                for event in self._events(item):
                    yield event
                    if isinstance(event, TurnEnd):
                        return
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.exception("agent turn failed")
            yield TurnEnd(self.session_id, f"{type(exc).__name__}: {exc}")
            return
        yield TurnEnd(self.session_id, "the agent stopped without finishing its turn")

    def _events(self, item: Any) -> list[Event]:
        """The gateway's events for one SDK message."""
        from claude_agent_sdk import (  # noqa: PLC0415  pylint: disable=import-outside-toplevel
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        events: list[Event] = []
        if isinstance(item, SystemMessage) and item.subtype == "init":
            self.session_id = item.data.get("session_id") or self.session_id
        elif isinstance(item, AssistantMessage):
            if getattr(item, "error", None):
                events.append(Notice(_assistant_error(item.error)))
            for block in item.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    events.append(Text(block.text))
                elif isinstance(block, ToolUseBlock):
                    events.append(ToolCall(block.id, block.name, dict(block.input or {})))
        elif isinstance(item, UserMessage) and isinstance(item.content, list):
            events += [
                ToolOutput(block.tool_use_id, _tool_output_text(block.content), bool(block.is_error))
                for block in item.content
                if isinstance(block, ToolResultBlock)
            ]
        elif isinstance(item, ResultMessage):
            self.session_id = item.session_id or self.session_id
            error = item.subtype if item.is_error or item.subtype != "success" else None
            events.append(TurnEnd(self.session_id, error, item.total_cost_usd))
        return events

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
                log.exception("agent disconnect failed")


ERRORS = {
    "rate_limit": "The agent hit its usage limit. It will continue when the limit resets; send a message then.",
    "authentication_failed": "The agent could not sign in. The gateway's Claude token needs renewing.",
    "billing_error": "The agent's account has a billing problem.",
}


def _assistant_error(kind: Any) -> str:
    return ERRORS.get(str(kind), f"The agent reported an error: {kind}.")
