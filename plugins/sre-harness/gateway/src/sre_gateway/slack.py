"""Slack, over Socket Mode: one outbound websocket, no inbound port, no request URL."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from sre_gateway.chat import ApprovalView, Click, Command, Handler, Incoming, NameTaken, Place, Posted

log = logging.getLogger(__name__)

APPROVE, DENY, DENY_REASON, REASON_VIEW = "sre_approve", "sre_deny", "sre_deny_reason", "sre_deny_reason_view"
IGNORED_INVITE_ERRORS = {"already_in_channel", "cant_invite_self"}


def escape(text: str) -> str:
    """Slack's three control characters, so text cannot form a mention or a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(text: str) -> str:
    """The agent writes Markdown; Slack bolds with single asterisks."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


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
        self._app_token = app_token
        self._client = self._app.client
        self._socket: AsyncSocketModeHandler | None = None
        self._stopped = asyncio.Event()
        self._bot_user: str | None = None
        self._names: dict[str, str] = {}

    async def start(self, handler: Handler) -> None:
        auth = await self._client.auth_test()
        self._bot_user = auth["user_id"]
        self._register(handler)
        self._socket = AsyncSocketModeHandler(self._app, self._app_token)
        await self._socket.connect_async()
        log.info("connected to Slack over Socket Mode")
        await self._stopped.wait()

    async def stop(self) -> None:
        if self._socket is not None:
            await self._socket.close_async()
        self._stopped.set()

    def _register(self, handler: Handler) -> None:
        app = self._app

        @app.event("message")
        async def _message(event: dict[str, Any]) -> None:
            if event.get("subtype") or event.get("bot_id") or not event.get("user"):
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
                )
            )

        @app.command("/incident")
        async def _incident(ack: Any, command: dict[str, Any]) -> None:
            await ack()
            text = command.get("text", "")
            await handler.on_command(Command("incident", command["user_id"], command["channel_id"], text))

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
            text=to_mrkdwn(text if raw else escape(text)),
            unfurl_links=False,
            unfurl_media=False,
        )
        return Posted(reply["channel"], reply["ts"])

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
