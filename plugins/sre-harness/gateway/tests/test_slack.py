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


def adapter(hosts=("github.com", "*.grafana.net")):
    instance = slack.SlackAdapter.__new__(slack.SlackAdapter)
    instance._client = RecordingClient()  # pylint: disable=protected-access
    instance._typing_refused = False  # pylint: disable=protected-access
    instance._link_hosts = frozenset(hosts)  # pylint: disable=protected-access
    instance._block_refused = set()  # pylint: disable=protected-access
    return instance


async def test_indicator_edits_are_escaped_and_deletes_name_the_message():
    chat = adapter()
    await chat.update(Posted("C1", "5.0"), "working <!channel>")
    await chat.delete(Posted("C1", "5.0"))
    calls = chat._client.calls  # pylint: disable=protected-access
    assert calls[0][1]["text"] == "working &lt;!channel&gt;"
    assert "<!channel>" not in calls[0][1]["blocks"][0]["text"]
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


ZW = "\u200b"
HOSTS = frozenset({"github.com", "*.grafana.net"})


def block(text, hosts=HOSTS):
    return slack.to_markdown_block(text, hosts)


def test_markdown_passes_through_for_slack_to_render():
    text = "# Title\n\n**bold** *italic* ~~gone~~\n\n> quote\n\n- a\n- b\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert block(text) == text


def test_a_listed_link_stays_clickable():
    assert (
        block("see [the PR](https://github.com/pgmac-net/x/pull/1)")
        == "see [the PR](https://github.com/pgmac-net/x/pull/1)"
    )
    assert block("[d](https://a.b.grafana.net/d/1)") == "[d](https://a.b.grafana.net/d/1)"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "http://github.com/x",
        "https://github.com@evil.example/x",
        "https://github.com.evil.example/x",
        "https://evilgithub.com/x",
        "https://grafana.net/x",
        "javascript:void",
    ],
)
def test_any_other_link_shows_its_address_and_is_not_clickable(url):
    assert block(f"[Grafana]({url})") == f"Grafana (`{url}`)"


def test_a_link_title_is_dropped_and_still_checked():
    assert block('[t](https://github.com/x "title")') == "[t](https://github.com/x)"
    assert block('[t](https://evil.example "title")') == "t (`https://evil.example`)"


def test_an_image_is_only_a_link():
    assert block("![logo](https://evil.example/a.png)") == "logo (`https://evil.example/a.png`)"


def test_a_bare_url_is_clickable_only_on_a_listed_host():
    assert block("see https://github.com/x.") == "see https://github.com/x."
    assert block("see https://evil.example/x, ok") == "see `https://evil.example/x`, ok"


def test_reference_links_are_defused():
    assert block("[a][1]\n[1]: https://evil.example") == "\\[a][1]\n\\[1]: `https://evil.example`"


def test_mention_syntax_cannot_form():
    out = block("hi <@U1> <!channel> <#C1> <https://evil.example|docs>")
    for token in ("<@", "<!", "<#", "<https"):
        assert token not in out
    assert out.count("<" + ZW) == 4


def test_ordinary_code_is_left_alone():
    text = "```bash\ncat <<EOF > f; a < b; xs[i](y)\n```\nand `kubectl get pods -o json | jq '.[0]'` here"
    assert block(text) == text


def test_code_keeps_everything_except_what_could_become_a_mention_or_link():
    out = block("```\n<!channel> [a](https://evil.example) <@U1>\n```\nand `<@U1>` here")
    for token in ("<!", "<@", "](https"):
        assert token not in out
    assert "<" + ZW + "!channel>" in out


def test_an_unclosed_inline_code_span_is_still_checked():
    assert "<@" not in block("oops `<@U1> never closed")


def test_the_default_allows_no_link():
    assert block("[a](https://github.com/x)", frozenset()) == "a (`https://github.com/x`)"


def test_very_long_lines_cannot_carry_a_link_or_mention():
    out = block("x" * 3000 + " <!here> [a](https://evil.example)")
    assert "<!here>" not in out
    assert "](https" not in out


@pytest.mark.parametrize("text", ["[" * 1500, "[a](" * 400, "http://" * 400, "`" * 1500, "<" * 1500])
def test_markdown_conversion_is_linear_on_hostile_text(text):
    started = time.monotonic()
    block(text)
    assert time.monotonic() - started < 1


async def test_agent_prose_goes_out_as_a_markdown_block_with_an_escaped_fallback():
    chat = adapter()
    await chat.post(Place("C1", "1.0"), "**hi** <!channel>")
    call = chat._client.calls[0]  # pylint: disable=protected-access
    assert call["blocks"] == [{"type": "markdown", "text": f"**hi** <{ZW}!channel>"}]
    assert "&lt;!channel&gt;" in call["text"]


async def test_a_raw_post_has_no_blocks():
    chat = adapter()
    await chat.post(Place("C1"), "plain", raw=True)
    assert "blocks" not in chat._client.calls[0]  # pylint: disable=protected-access


async def test_text_over_the_block_limit_goes_out_as_text():
    chat = adapter()
    await chat.post(Place("C1"), "a" * (slack.MARKDOWN_BLOCK_LIMIT + 1))
    assert "blocks" not in chat._client.calls[0]  # pylint: disable=protected-access


async def test_a_refused_block_is_resent_as_text_and_logged_once(caplog):
    chat = adapter()
    original = chat._client.chat_postMessage  # pylint: disable=protected-access

    async def refuse_blocks(**kwargs):
        if "blocks" in kwargs:
            raise SlackApiError("no", {"ok": False, "error": "invalid_blocks"})
        return await original(**kwargs)

    chat._client.chat_postMessage = refuse_blocks  # pylint: disable=protected-access,invalid-name
    with caplog.at_level("WARNING"):
        await chat.post(Place("C1"), "one")
        await chat.post(Place("C1"), "two")
    assert [c["text"] for c in chat._client.calls] == ["one", "two"]  # pylint: disable=protected-access
    assert sum("invalid_blocks" in r.message for r in caplog.records) == 1


async def test_an_edit_sends_a_block_and_a_refused_one_clears_the_old_blocks():
    chat = adapter()
    await chat.update(Posted("C1", "5.0"), "now **this**")
    assert chat._client.calls[0][1]["blocks"] == [{"type": "markdown", "text": "now **this**"}]  # pylint: disable=protected-access
    original = chat._client.chat_update  # pylint: disable=protected-access

    async def refuse_blocks(**kwargs):
        if kwargs["blocks"]:
            raise SlackApiError("no", {"ok": False, "error": "invalid_blocks"})
        await original(**kwargs)

    chat._client.chat_update = refuse_blocks  # pylint: disable=protected-access
    await chat.update(Posted("C1", "5.0"), "again")
    assert chat._client.calls[-1][1]["blocks"] == []  # pylint: disable=protected-access


@pytest.mark.parametrize(
    "text",
    [
        "\\`<@U1>\\`",  # an escaped backtick opens nothing
        "``x` <@U1> `",  # runs of different length do not pair
        "`a``b` <@U1>",
        "~~~\n```\n~~~\n<@U1>",  # a backtick line does not close a tilde fence
        "````\n```\n````\n<@U1>",  # a shorter fence does not close a longer one
        "```\n``` not a close\n```\n<@U1>",  # a closing fence has nothing after it
        "    ```\n<@U1>",  # four spaces of indent is not a fence
        "```<@U1>",  # the info string is not prose
    ],
)
def test_no_reading_of_the_code_structure_lets_a_mention_through(text):
    assert "<@" not in block(text)


def test_tilde_and_longer_fences_hold_their_content_as_code():
    text = "~~~\n[a](x) <b>\n~~~\n````\n```\n<b> y\n````"
    assert block(text) == text


def test_a_bare_www_address_is_clickable_only_on_a_listed_host():
    assert block("see www.evil.example/x now") == "see `www.evil.example/x` now"
    assert block("see www.grafana.net/x") == "see www.grafana.net/x"
    assert block("awww.evil.example") == "awww.evil.example"
