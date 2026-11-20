"""Slack, over Socket Mode: one outbound websocket, no inbound port, no request URL."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from sre_gateway.chat import (
    ApprovalView,
    Click,
    Command,
    FileRef,
    FileUnavailable,
    Handler,
    Incoming,
    NameTaken,
    Place,
    Posted,
)

log = logging.getLogger(__name__)

APPROVE, DENY, DENY_REASON, REASON_VIEW = "sre_approve", "sre_deny", "sre_deny_reason", "sre_deny_reason_view"
IGNORED_INVITE_ERRORS = {"already_in_channel", "cant_invite_self"}
TYPING = "is writing…"  # Slack puts the app's name in front.
# A message with an attachment has this subtype; every other subtype (edits, joins, bot posts) is ignored.
FILE_SHARE = "file_share"
DOWNLOAD_SECONDS = 30
NO_ACCESS = "Slack would not give the file to the app (does the app have the files:read scope?)"


def file_refs(event: dict[str, Any]) -> tuple[FileRef, ...]:
    """The files on a message event. A file without a download address is left out; nothing can be read from it."""
    refs = []
    for item in event.get("files") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url_private_download") or item.get("url_private")
        if not url:
            continue
        refs.append(
            FileRef(
                id=str(item.get("id", "")),
                name=str(item.get("name") or item.get("title") or "file"),
                size=int(item.get("size") or 0),
                mimetype=str(item.get("mimetype") or ""),
                snippet=item.get("mode") == "snippet",
                url=url,
            )
        )
    return tuple(refs)


def escape(text: str) -> str:
    """Slack's three control characters, so text cannot form a mention or a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The agent's text is untrusted input to these checks, so none of them uses a pattern
# that can backtrack: rows and headings are parsed with string operations, the one
# pattern left is anchored and unambiguous, and lines longer than this are passed
# through untouched.
MAX_CONVERTED_LINE = 2000
RULE_CELL = re.compile(r":?-{3,}:?")


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|")


def _is_table_rule(line: str) -> bool:
    """The '|---|:---:|' line under a table's header."""
    stripped = line.strip().strip("|")
    return bool(stripped) and all(RULE_CELL.fullmatch(cell.strip()) for cell in stripped.split("|"))


def _heading(line: str) -> str | None:
    """The text of a Markdown heading ('# Title', '## Title ##'), or None."""
    level = len(line) - len(line.lstrip("#"))
    if not 1 <= level <= 6 or line[level : level + 1] not in (" ", "\t"):
        return None
    text = line[level:].strip().rstrip("#").strip()
    return text or None


def _bold(line: str) -> str:
    """Markdown '**bold**' to Slack '*bold*'. An unpaired '**' is left as it is."""
    parts = line.split("**")
    if len(parts) < 3:
        return line
    paired = len(parts) if len(parts) % 2 else len(parts) - 1
    return "*".join(parts[:paired]) + "".join(f"**{part}" for part in parts[paired:])


def _cells(row: str) -> list[str]:
    """A table row's cells, without the formatting a code block would show literally."""
    cells = row.strip().strip("|").split("|")
    return [cell.replace("**", "").replace("`", "").strip() for cell in cells]


def _table_block(rows: list[str]) -> list[str]:
    """A Markdown table as an aligned fixed-width block: Slack has no table syntax."""
    table = [_cells(row) for row in rows if not _is_table_rule(row)]
    width = max(len(row) for row in table)
    table = [row + [""] * (width - len(row)) for row in table]
    sizes = [max(len(row[i]) for row in table) for i in range(width)]
    lines = ["  ".join(cell.ljust(sizes[i]) for i, cell in enumerate(row)).rstrip() for row in table]
    return ["```", *lines, "```"]


def to_mrkdwn(text: str) -> str:
    """The agent writes Markdown; turn it into what Slack renders.

    Bold uses single asterisks, a heading becomes a bold line, and a table becomes an
    aligned fixed-width block. Anything inside an existing code block is left alone.
    """
    out: list[str] = []
    lines = text.split("\n")
    in_code = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            index += 1
            continue
        if in_code or len(line) > MAX_CONVERTED_LINE:
            out.append(line)
            index += 1
            continue
        if _is_table_row(line) and index + 1 < len(lines) and _is_table_rule(lines[index + 1]):
            end = index
            while end < len(lines) and len(lines[end]) <= MAX_CONVERTED_LINE and _is_table_row(lines[end]):
                end += 1
            out.extend(_table_block(lines[index:end]))
            index = end
            continue
        heading = _heading(line)
        out.append(f"*{heading}*" if heading else _bold(line))
        index += 1
    return "\n".join(out)


def _request_blocks(view: ApprovalView, footer: str, buttons: bool) -> list[dict[str, Any]]:
    """The request. What will run is a literal preformatted block: nothing in it can format, mention or link.

    The agent's description and the outcome are plain text for the same reason.
    """
    header = f"*Approval needed* for `{escape(view.tool)}`. This will run:"
    if view.statement_link:
        header += f"\n<{view.statement_link}|The agent's statement of the command and its blast radius>"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "rich_text",
            "elements": [{"type": "rich_text_preformatted", "elements": [{"type": "text", "text": view.summary}]}],
        },
    ]
    if view.description:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "plain_text", "text": f"Agent's description (unverified): {view.description}"}],
            }
        )
    blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": footer}]})
    if buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": APPROVE,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": view.request_id,
                    },
                    {
                        "type": "button",
                        "action_id": DENY,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "value": view.request_id,
                    },
                    {
                        "type": "button",
                        "action_id": DENY_REASON,
                        "text": {"type": "plain_text", "text": "Deny with reason"},
                        "value": view.request_id,
                    },
                ],
            }
        )
    return blocks


class SlackAdapter:
    def __init__(self, bot_token: str, app_token: str):
        self._app = AsyncApp(token=bot_token)
        self._bot_token = bot_token
        self._app_token = app_token
        self._client = self._app.client
        self._socket: AsyncSocketModeHandler | None = None
        self._stopped = asyncio.Event()
        self._bot_user: str | None = None
        self._names: dict[str, str] = {}
        self._typing_refused = False

    async def start(self, handler: Handler) -> None:
        auth = await self._client.auth_test()
        self._bot_user = auth["user_id"]
        self._register(handler)
        self._socket = AsyncSocketModeHandler(self._app, self._app_token)
        await self._socket.connect_async()
        log.info("connected to Slack over Socket Mode")
        await self._stopped.wait()

    async def download(self, file: FileRef, max_bytes: int) -> bytes:
        host = urlparse(file.url).hostname or ""
        if not host.endswith(".slack.com"):
            raise FileUnavailable("the file is not on Slack's file servers")
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # No redirects: the token goes only to the host checked above.
            async with session.get(
                file.url, headers={"Authorization": f"Bearer {self._bot_token}"}, allow_redirects=False
            ) as response:
                kind = response.headers.get("Content-Type", "")
                refused = kind.startswith("text/html") and not file.mimetype.startswith("text/html")
                if response.status != 200 or refused:
                    raise FileUnavailable(NO_ACCESS)
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise FileUnavailable("the file is larger than the download limit")
        return bytes(data)

    async def stop(self) -> None:
        if self._socket is not None:
            await self._socket.close_async()
        self._stopped.set()

    def _register(self, handler: Handler) -> None:
        app = self._app

        @app.event("message")
        async def _message(event: dict[str, Any]) -> None:
            if event.get("subtype") not in (None, FILE_SHARE) or event.get("bot_id") or not event.get("user"):
                return
            text = event.get("text") or ""
            mention = f"<@{self._bot_user}>"
            await handler.on_message(
                Incoming(
                    channel=event["channel"],
                    user=event["user"],
                    text=text.replace(mention, "").strip(),
                    ts=event["ts"],
                    thread=event.get("thread_ts"),
                    mentions_bot=mention in text,
                    files=file_refs(event) if event.get("subtype") == FILE_SHARE else (),
                )
            )

        @app.command("/incident")
        async def _incident(ack: Any, command: dict[str, Any]) -> None:
            await ack()
            text = command.get("text", "")
            await handler.on_command(Command("incident", command["user_id"], command["channel_id"], text))

        @app.command("/sre")
        async def _sre(ack: Any, command: dict[str, Any]) -> None:
            await ack()
            text = command.get("text", "")
            await handler.on_command(Command("sre", command["user_id"], command["channel_id"], text))

        @app.action(APPROVE)
        async def _approve(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await handler.on_click(Click(body["actions"][0]["value"], body["user"]["id"], "approve"))

        @app.action(DENY)
        async def _deny(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await handler.on_click(Click(body["actions"][0]["value"], body["user"]["id"], "deny"))

        @app.action(DENY_REASON)
        async def _deny_reason(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            modal = _reason_modal(body["actions"][0]["value"])
            await self._client.views_open(trigger_id=body["trigger_id"], view=modal)

        @app.view(REASON_VIEW)
        async def _reason(ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
            await ack()
            reason = view["state"]["values"]["reason"]["reason"].get("value") or ""
            await handler.on_click(Click(view["private_metadata"], body["user"]["id"], "deny", reason.strip()))

        @app.error
        async def _error(error: Exception) -> None:
            log.error("Slack handler failed: %s", type(error).__name__)

    async def post(self, place: Place, text: str, raw: bool = False) -> Posted:
        reply = await self._client.chat_postMessage(
            channel=place.channel,
            thread_ts=place.thread,
            text=to_mrkdwn(text) if raw else escape(to_mrkdwn(text)),
            unfurl_links=False,
            unfurl_media=False,
        )
        return Posted(reply["channel"], reply["ts"])

    async def update(self, posted: Posted, text: str) -> None:
        await self._client.chat_update(channel=posted.channel, ts=posted.ts, text=escape(to_mrkdwn(text)))

    async def delete(self, posted: Posted) -> None:
        await self._client.chat_delete(channel=posted.channel, ts=posted.ts)

    async def set_typing(self, place: Place, on: bool) -> None:
        """Slack's AI-app status ('sre-harness is writing…'). It exists only in a thread.

        If Slack refuses it (the plan, the app's features or the channel type), it stays off until
        the gateway restarts, and the refusal is logged once.
        """
        if self._typing_refused or not place.thread:
            return
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=place.channel,
                thread_ts=place.thread,
                status=TYPING if on else "",
                loading_messages=[TYPING] if on else None,
            )
        except SlackApiError as exc:
            self._typing_refused = True
            log.warning(
                "Slack refused the typing indicator (%s); it stays off until restart", exc.response.get("error")
            )

    async def permalink(self, posted: Posted) -> str | None:
        reply = await self._client.chat_getPermalink(channel=posted.channel, message_ts=posted.ts)
        return reply.get("permalink")

    async def post_approval(self, place: Place, view: ApprovalView) -> Posted:
        footer = f"Expires in {max(1, view.expires_in_seconds // 60)} min. Only listed approvers can answer."
        reply = await self._client.chat_postMessage(
            channel=place.channel,
            thread_ts=place.thread,
            text=f"Approval needed for {view.tool}",
            blocks=_request_blocks(view, footer, buttons=True),
        )
        return Posted(reply["channel"], reply["ts"])

    async def close_approval(self, posted: Posted, view: ApprovalView, outcome: str) -> None:
        await self._client.chat_update(
            channel=posted.channel,
            ts=posted.ts,
            text=escape(f"Approval request for {view.tool}: {outcome}"),
            blocks=_request_blocks(view, outcome, buttons=False),
        )

    async def tell(self, place: Place, user: str, text: str) -> None:
        await self._client.chat_postEphemeral(
            channel=place.channel, user=user, text=escape(text), thread_ts=place.thread
        )

    async def create_private_channel(self, name: str) -> str:
        try:
            reply = await self._client.conversations_create(name=name, is_private=True)
        except SlackApiError as exc:
            if exc.response.get("error") == "name_taken":
                raise NameTaken(name) from exc
            raise
        return reply["channel"]["id"]

    async def invite(self, channel: str, users: list[str]) -> None:
        others = [user for user in users if user != self._bot_user]
        if not others:
            return
        try:
            await self._client.conversations_invite(channel=channel, users=",".join(others))
        except SlackApiError as exc:
            if exc.response.get("error") not in IGNORED_INVITE_ERRORS:
                raise

    async def archive(self, channel: str) -> None:
        await self._client.conversations_archive(channel=channel)

    async def user_name(self, user: str) -> str:
        if user not in self._names:
            reply = await self._client.users_info(user=user)
            profile = reply["user"].get("profile", {})
            self._names[user] = profile.get("display_name") or profile.get("real_name") or user
        return self._names[user]

    def channel_link(self, channel: str) -> str:
        return f"<#{channel}>"

    def escape(self, text: str) -> str:
        return escape(text)


def _reason_modal(request_id: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": REASON_VIEW,
        "private_metadata": request_id,
        "title": {"type": "plain_text", "text": "Deny with reason"},
        "submit": {"type": "plain_text", "text": "Deny"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "Why not?"},
                "element": {"type": "plain_text_input", "action_id": "reason", "multiline": True, "max_length": 500},
            }
        ],
    }
