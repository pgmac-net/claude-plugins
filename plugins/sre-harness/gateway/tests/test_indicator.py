"""The working and typing indicators: labels, ordering around posts, heartbeat, approvals and failures."""

from __future__ import annotations

import asyncio

import pytest

from fakes import FakeChat, settle
from sre_gateway.agent import Text, ToolCall, ToolOutput
from sre_gateway.chat import Click, Incoming, Place
from sre_gateway.indicator import IndicatedChat, activity_for, elapsed
from test_gateway import OWNER, make, opened, start_incident  # pylint: disable=wrong-import-order

SECRETISH = "xoxb-" + "123456789012-abcdefghijkl"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


# --- labels -----------------------------------------------------------------------


def test_activity_labels_never_carry_tool_input():
    assert activity_for("Read", {"file_path": "/etc/shadow"}) == "reading files"
    assert activity_for("Grep", {"pattern": SECRETISH}) == "reading files"
    assert activity_for("Skill", {"skill": "start-incident"}) == "using a skill"
    assert activity_for("Bash", {"command": "uv run /opt/p/scripts/kube_read.py get pods -n web"}) == (
        "running kube_read.py"
    )
    assert activity_for("Bash", {"command": "uv run '/opt/p/scripts/incident.py' open --title x"}) == (
        "running incident.py"
    )
    assert activity_for("Bash", {"command": f"curl -H 'Authorization: {SECRETISH}' https://x"}) == (
        "running a shell command"
    )
    # A script of the same name in a command that is not the harness's still names only the file.
    assert activity_for("Bash", {"command": "cat notes.py"}) == "running a shell command"
    assert activity_for("mcp__nagios__get_alerts", {"host": "web01"}) == "using nagios get_alerts"
    assert activity_for("mcp__x<y>__z", {}) == "using z"
    assert activity_for("Weird <@U123> tool", {}) == "using a tool"
    assert activity_for("TodoWrite", {"todos": [SECRETISH]}) == "using TodoWrite"


def test_elapsed():
    assert elapsed(4.9) == "4s"
    assert elapsed(80) == "1m 20s"
    assert elapsed(3720) == "1h 2m"


# --- the indicator on its own, with a clock ----------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fast_ticks(monkeypatch):
    monkeypatch.setattr("sre_gateway.indicator.TICK_SECONDS", 0)


@pytest.mark.usefixtures("fast_ticks")
async def test_heartbeat_edits_elapsed_and_refreshes_typing():
    clock, raw = Clock(), FakeChat()
    chat = IndicatedChat(raw, clock)
    place = Place("C1", "1.0")
    indicator = await chat.open(lambda: place)
    assert raw.lines[-1][2] == "sre-harness is starting… (0s)"
    assert raw.typing == [(place, True)]
    await settle()
    assert raw.edits == []  # nothing changed, no heartbeat due
    clock.now += 31
    await settle()
    assert raw.edits[-1][1] == "sre-harness is starting… (31s)"
    edits = len(raw.edits)
    await settle()
    assert len(raw.edits) == edits  # one edit per heartbeat, not per tick
    indicator.set_activity("running kube_read.py")
    await settle()
    assert raw.edits[-1][1] == "sre-harness is working: running kube_read.py… (31s)"
    clock.now += 60
    await settle()
    assert raw.typing[-1] == (place, True) and len(raw.typing) == 2  # refreshed before Slack drops it
    await chat.close(indicator)
    assert raw.typing[-1] == (place, False)
    assert raw.live_lines() == []


async def test_failed_delete_marks_the_line_finished():
    raw = FakeChat()
    chat = IndicatedChat(raw)
    indicator = await chat.open(lambda: Place("C1"))

    async def refuse(_posted):
        raise RuntimeError("cant_delete_message")

    raw.delete = refuse
    await chat.close(indicator)
    assert raw.edits[-1][1] == "sre-harness finished."


async def test_posts_elsewhere_do_not_move_the_line():
    raw = FakeChat()
    chat = IndicatedChat(raw)
    indicator = await chat.open(lambda: Place("C1", "1.0"))
    await chat.post(Place("C1"), "hub announcement")
    await chat.post(Place("C1", "2.0"), "another thread")
    assert len(raw.lines) == 1 and raw.deleted == []
    await chat.close(indicator)


# --- through the gateway ----------------------------------------------------------


def check_ordering(chat: FakeChat) -> None:
    """Typing is never showing in a place when something is posted there."""
    typing: dict[Place, bool] = {}
    for index, (kind, value, place) in enumerate(chat.timeline):
        if kind == "typing":
            typing[place] = value == "on"
        elif kind in ("post", "approval"):
            assert not typing.get(place), f"typing showed over post {index}: {value}"


async def test_a_turn_shows_both_indicators_and_clears_them(state):
    log = {}
    hold = asyncio.Event()
    script = [[Text("Checking pods."), ("wait", hold), Text("Pods are fine.")]]
    gateway, chat, _t, _r = make(state, script, log)
    await start_incident(gateway)
    # Mid-turn: one live line, and it is the last thing in the conversation.
    assert len(chat.live_lines()) == 1
    last_post = max(i for i, (kind, _v, _p) in enumerate(chat.timeline) if kind == "post")
    last_line = max(i for i, (kind, _v, _p) in enumerate(chat.timeline) if kind == "line")
    assert last_line > last_post
    assert chat.typing[-1][1] is True
    hold.set()
    await settle()
    assert "Pods are fine." in chat.texts()[-1]
    assert chat.live_lines() == []
    assert chat.typing[-1][1] is False
    assert chat.lines[0][2].startswith("sre-harness is starting…")
    check_ordering(chat)
    await gateway.shutdown()


@pytest.mark.usefixtures("fast_ticks")
async def test_tool_calls_change_the_label(state):
    hold = asyncio.Event()
    command = {"command": "uv run /opt/p/scripts/kube_read.py get pods"}
    script = [[ToolCall("t1", "Bash", command), ("wait", hold), ToolOutput("t1", "ok", False)]]
    gateway, chat, _t, _r = make(state, script, {})
    await start_incident(gateway)
    await settle()
    assert chat.edits[-1][1].startswith("sre-harness is working: running kube_read.py…")
    hold.set()
    await settle()
    assert chat.live_lines() == []
    await gateway.shutdown()


async def test_approval_wait_clears_typing_and_says_so(state):
    script = [[Text("I will restart web."), ("ask", "Bash", {"command": "touch x"}, "t-9"), Text("Done.")]]
    gateway, chat, _t, _r, session, _log = await opened(state, script)
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "fix it", "50.0"))
    await settle()
    _place, view, _posted = chat.approvals[-1]
    assert "waiting for approval" in chat.edits[-1][1]
    assert chat.typing[-1][1] is False
    # The line moved below the request, and typing stayed off.
    assert chat.timeline[-1][0] == "line"
    await gateway.on_click(Click(view.request_id, OWNER, "approve"))
    await settle()
    assert "Done." in chat.texts()[-1]
    assert chat.live_lines() == []
    check_ordering(chat)
    await gateway.shutdown()


async def test_failed_start_removes_the_line(state, monkeypatch):
    monkeypatch.setattr("sre_gateway.gateway.START_RETRY_SECONDS", 0)
    gateway, chat, _t, _r = make(state, [[Text("on it")]], {"fail_starts": 3})
    await start_incident(gateway)
    await settle(40)
    assert any("did not start after 3 tries" in text for text in chat.texts())
    assert chat.lines and chat.live_lines() == []
    assert chat.typing[-1][1] is False
    await gateway.shutdown()


async def test_indicator_failures_never_stop_the_turn(state):
    chat = FakeChat(fail_indicator=True)
    gateway, chat, _t, _r = make(state, [[Text("On it."), Text("Found it.")]], {}, chat=chat)
    await start_incident(gateway)
    assert "Found it." in chat.texts()[-1]
    await gateway.shutdown()


async def test_the_line_follows_the_conversation_into_its_channel(state):
    gateway, chat, _t, _r, session, _log = await opened(state, [[Text("Looking.")]])
    channel = session.conv.channel
    await gateway.on_message(Incoming(channel, OWNER, "go on", "50.0"))
    await settle()
    assert chat.lines[-1][0] == Place(channel)
    assert chat.live_lines() == []
    await gateway.shutdown()
