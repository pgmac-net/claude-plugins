"""Slack formatting: agent text cannot mention, link or break out of the request's command block."""

from __future__ import annotations

import time

import pytest
from slack_sdk.errors import SlackApiError

from sre_gateway import slack
from sre_gateway.chat import ApprovalView, Place, Posted


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802  pylint: disable=invalid-name
        self.calls.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1.0"}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs))

    async def chat_delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    async def assistant_threads_setStatus(self, **kwargs):  # noqa: N802  pylint: disable=invalid-name
        self.calls.append(("status", kwargs))
        if self.refuse:
            raise SlackApiError("refused", {"ok": False, "error": "team_not_on_paid_plan"})

    refuse = False


def adapter():
    instance = slack.SlackAdapter.__new__(slack.SlackAdapter)
    instance._client = RecordingClient()  # pylint: disable=protected-access
    instance._typing_refused = False  # pylint: disable=protected-access
    return instance


async def test_indicator_edits_are_escaped_and_deletes_name_the_message():
    chat = adapter()
    await chat.update(Posted("C1", "5.0"), "working <!channel>")
    await chat.delete(Posted("C1", "5.0"))
    calls = chat._client.calls  # pylint: disable=protected-access
    assert calls[0] == ("update", {"channel": "C1", "ts": "5.0", "text": "working &lt;!channel&gt;"})
    assert calls[1] == ("delete", {"channel": "C1", "ts": "5.0"})


async def test_typing_is_set_only_in_a_thread_and_cleared_with_an_empty_status():
    chat = adapter()
    await chat.set_typing(Place("C1"), True)
    assert chat._client.calls == []  # pylint: disable=protected-access
    await chat.set_typing(Place("C1", "1.0"), True)
    await chat.set_typing(Place("C1", "1.0"), False)
    on, off = (kwargs for _kind, kwargs in chat._client.calls)  # pylint: disable=protected-access
    assert on == {"channel_id": "C1", "thread_ts": "1.0", "status": "is writing…", "loading_messages": ["is writing…"]}
    assert off == {"channel_id": "C1", "thread_ts": "1.0", "status": "", "loading_messages": None}


async def test_a_refused_typing_status_stays_off_until_restart():
    chat = adapter()
    chat._client.refuse = True  # pylint: disable=protected-access
    await chat.set_typing(Place("C1", "1.0"), True)
    await chat.set_typing(Place("C1", "1.0"), True)
    assert len(chat._client.calls) == 1  # pylint: disable=protected-access


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


TABLE = """Here it is:

| SLO | Target / window | State |
|---|---|---|
| ingress-open-internal-web-app (**spends budget**) | 99.5% / 7d | `healthy` |
| ingress-requests | 99% / 7d | informational |

Done."""


def test_a_markdown_table_becomes_an_aligned_block():
    rendered = slack.to_mrkdwn(TABLE)
    assert "|" not in rendered
    assert "---" not in rendered
    block = rendered.split("```")[1].strip("\n").splitlines()
    assert block[0].startswith("SLO")
    assert block[1].startswith("ingress-open-internal-web-app (spends budget)")
    assert block[0].index("Target") == block[1].index("99.5%") == block[2].index("99% / 7d")
    assert rendered.startswith("Here it is:") and rendered.endswith("Done.")


def test_headings_become_bold_lines():
    assert slack.to_mrkdwn("## Where it stands\nok") == "*Where it stands*\nok"


def test_code_blocks_are_left_alone():
    text = "```\n| a | b |\n|---|---|\n**x**\n```"
    assert slack.to_mrkdwn(text) == text


def test_pipes_in_prose_are_not_a_table():
    assert slack.to_mrkdwn("a | b") == "a | b"


async def test_table_cells_are_escaped_after_alignment():
    chat = adapter()
    await chat.post(Place("C1"), "| a | b |\n|---|---|\n| x > y | <!channel> |")
    text = chat._client.calls[0]["text"]  # pylint: disable=protected-access
    assert "<!channel>" not in text
    assert "x &gt; y" in text


@pytest.mark.parametrize(
    "text",
    [
        "# a" + " " * 20000 + "b" + " " * 20000 + "#x",
        "## " + "# " * 20000,
        "**a" * 20000,
        "**" + "a " * 20000 + "*",
        "|" + " |" * 20000,
        "| a |\n|" + "-" * 20000 + "|x",
        ("| a | b |\n|---|---|\n" + "| x | y |\n" * 2000),
    ],
)
def test_conversion_takes_linear_time_on_hostile_text(text):
    """The agent's text is untrusted: no input may make the gateway hang."""
    started = time.perf_counter()
    slack.to_mrkdwn(text)
    assert time.perf_counter() - started < 1.0


def test_bold_pairs_and_leaves_an_unpaired_marker():
    assert slack.to_mrkdwn("**a** and **b** and **c") == "*a* and *b* and **c"


def test_heading_with_closing_hashes():
    assert slack.to_mrkdwn("### Budget ###") == "*Budget*"
    assert slack.to_mrkdwn("#not a heading") == "#not a heading"
    assert slack.to_mrkdwn("####### seven") == "####### seven"


def test_very_long_lines_pass_through():
    line = "**" + "x" * 3000 + "**"
    assert slack.to_mrkdwn(line) == line
