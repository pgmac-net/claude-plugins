"""Slack formatting: agent text cannot mention, link or break out of the request's command block."""

from __future__ import annotations

from sre_gateway import slack
from sre_gateway.chat import ApprovalView, Place


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802  pylint: disable=invalid-name
        self.calls.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1.0"}


def adapter():
    instance = slack.SlackAdapter.__new__(slack.SlackAdapter)
    instance._client = RecordingClient()  # pylint: disable=protected-access
    return instance


async def test_agent_text_is_escaped_so_it_cannot_mention_or_link():
    chat = adapter()
    await chat.post(Place("C1"), "hey <!channel> see <https://evil.example|docs> & **bold**")
    text = chat._client.calls[0]["text"]  # pylint: disable=protected-access
    assert "<!channel>" not in text
    assert "<https://evil" not in text
    assert "&lt;!channel&gt;" in text
    assert "*bold*" in text


async def test_raw_post_keeps_gateway_links():
    chat = adapter()
    await chat.post(Place("C1"), f"Continuing in {chat.channel_link('C2')}.", raw=True)
    assert "<#C2>" in chat._client.calls[0]["text"]  # pylint: disable=protected-access


def test_request_shows_the_command_literally_and_the_description_apart():
    view = ApprovalView(
        request_id="r1",
        tool="Bash",
        summary="echo ```\n*fake* <!channel>",
        statement_link="https://chat.example.com/p1",
        expires_in_seconds=300,
        description="harmless, trust me <!here>",
    )
    blocks = slack._request_blocks(view, "Expires in 5 min.", buttons=True)  # pylint: disable=protected-access
    preformatted = blocks[1]["elements"][0]
    assert preformatted["type"] == "rich_text_preformatted"
    assert preformatted["elements"][0] == {"type": "text", "text": view.summary}
    description = blocks[2]["elements"][0]
    assert description["type"] == "plain_text"
    assert description["text"].startswith("Agent's description (unverified)")
    assert [element["action_id"] for element in blocks[-1]["elements"]] == [
        slack.APPROVE,
        slack.DENY,
        slack.DENY_REASON,
    ]
    assert all(view.summary not in str(block) for block in blocks if block["type"] == "section")


def test_closed_request_has_no_buttons():
    view = ApprovalView("r1", "Bash", "touch x", None, 300)
    blocks = slack._request_blocks(view, "Approved by someone.", buttons=False)  # pylint: disable=protected-access
    assert all(block["type"] != "actions" for block in blocks)
