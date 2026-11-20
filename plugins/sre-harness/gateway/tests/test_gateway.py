"""The gateway's routing, incident lifecycle, sessions and limits, against fakes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fakes import FakeChat, FakeTracker, driver_factory, settle
from sre_gateway.agent import Notice, Text, ToolCall, ToolOutput, TurnEnd
from sre_gateway.chat import Click, Command, Incoming
from sre_gateway.gateway import Gateway, Settings, channel_name, chunks
from sre_gateway.store import Store

HUB = "C" + "0HUB00001"
OWNER = "U" + "0OWNER001"
OTHER = "U" + "0OTHER001"
STRANGER = "U" + "0STRANGE1"
TOKEN = "xoxb-" + "123456789012-abcdefghijkl"
OPEN_CMD = "uv run /opt/plugin/scripts/incident.py open --title 'Web down' --severity P2"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


def write_incident(state: Path, number: int = 7, drill: bool = False, title: str = "Web down") -> dict:
    incident = {
        "id": f"incidents-{number}",
        "url": f"https://example.com/issues/{number}",
        "number": number,
        "title": f"[DRILL] {title}" if drill else title,
        "drill": drill,
        "status": "open",
    }
    path = state / "xdg" / "sre-harness" / "incidents" / f"{incident['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(incident), encoding="utf-8")
    return incident


def open_events(incident: dict, call_id: str = "t-open") -> list:
    out = json.dumps({"id": incident["id"], "url": incident["url"], "bind": "0" * 32})
    return [ToolCall(call_id, "Bash", {"command": OPEN_CMD}), ToolOutput(call_id, out, False)]


def make(state, script, log, **overrides):
    settings = Settings(
        hub=HUB,
        responders=frozenset({OWNER, OTHER}),
        approvers=frozenset({OWNER}),
        approval_ttl=overrides.pop("ttl", 60),
        idle_seconds=overrides.pop("idle", 600),
        max_sessions=overrides.pop("max_sessions", 2),
        state_dir=state / "gateway",
        question_idle_seconds=overrides.pop("question_idle", 600),
    )
    chat, tracker = overrides.pop("chat", FakeChat()), overrides.pop("tracker", FakeTracker())
    records = []
    gateway = Gateway(
        settings,
        chat=chat,
        tracker=tracker,
        driver_factory=driver_factory(script, log),
        record=records.append,
        scrub=lambda text: text.replace(TOKEN, "[REDACTED]"),
        runner=overrides.pop("runner", None),
        record_policy=overrides.pop("record_policy", None),
        env_values=overrides.pop("env_values", None),
    )
    return gateway, chat, tracker, records


async def start_incident(gateway, text="Web down", user=OWNER):
    await gateway.on_command(Command("incident", user, HUB, text))
    await settle()


# --- pure helpers ---------------------------------------------------------------


def test_channel_names():
    assert channel_name({"number": 7, "title": "Web is DOWN!"}) == "inc-7-web-is-down"
    assert channel_name({"number": 3, "title": "[DRILL] Jiva stuck", "drill": True}) == "drill-3-jiva-stuck"
    drill = {"number": 14, "title": "[DRILL] Drill: gateway end-to-end test", "drill": True}
    assert channel_name(drill) == "drill-14-gateway-end-to-end-test"
    assert channel_name({"number": 5, "title": "Drilling rig offline"}) == "inc-5-drilling-rig-offline"
    assert channel_name({"number": 7, "title": "x"}, 2) == "inc-7-x-2"
    assert len(channel_name({"number": 1, "title": "y" * 300})) <= 80


def test_chunks_split_long_text():
    parts = chunks("line\n" * 2000, size=100)
    assert all(len(part) <= 100 for part in parts)
    assert "".join(parts) == "line\n" * 2000


# --- starting -------------------------------------------------------------------


async def test_responder_command_starts_a_conversation(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text("On it.")]], log)
    await start_incident(gateway)
    assert "name-" + OWNER in chat.posts[0][1]
    assert chat.posts[0][0].channel == HUB
    assert "started an incident: Web down" in log["messages"][0]
    assert "start-incident skill" in log["messages"][0]
    assert "On it." in chat.texts()[-1]
    await gateway.shutdown()


async def test_drill_prefix_marks_a_drill(state):
    log = {}
    gateway, _c, _t, _r = make(state, [[]], log)
    await start_incident(gateway, "drill: jiva stuck")
    assert "started a drill: jiva stuck" in log["messages"][0]
    assert "--drill" in log["messages"][0]
    assert next(iter(gateway.sessions.values())).conv.drill
    await gateway.shutdown()


async def test_non_responder_cannot_start(state):
    log = {}
    gateway, chat, _t, _r = make(state, [], log)
    await start_incident(gateway, user=STRANGER)
    assert gateway.sessions == {}
    assert chat.told and "Only listed responders" in chat.told[0][1]


async def test_hub_mention_starts_and_plain_chat_does_not(state):
    log = {}
    gateway, _c, _t, _r = make(state, [[]], log)
    await gateway.on_message(Incoming(HUB, OWNER, "just chatting", "1.0"))
    assert gateway.sessions == {}
    await gateway.on_message(Incoming(HUB, OWNER, "web is down", "2.0", mentions_bot=True))
    await settle()
    assert len(gateway.sessions) == 1
    await gateway.shutdown()


async def test_non_responder_messages_never_reach_the_agent(state):
    log = {}
    gateway, _c, _t, _r = make(state, [[]], log)
    await start_incident(gateway)
    conv = next(iter(gateway.sessions.values())).conv
    await gateway.on_message(Incoming(conv.channel, STRANGER, "rm everything", "9.0", thread=conv.thread))
    await settle()
    assert all("rm everything" not in m for m in log["messages"])
    await gateway.shutdown()


# --- opening the record moves the conversation ----------------------------------


async def test_opened_incident_gets_a_private_channel(state):
    incident = write_incident(state)
    log = {}
    gateway, chat, _t, _r = make(state, [open_events(incident)], log)
    await start_incident(gateway)
    assert chat.channels == ["inc-7-web-down"]
    session = next(iter(gateway.sessions.values()))
    assert session.conv.incident == "incidents-7"
    assert session.conv.thread is None
    assert sorted(chat.invited[session.conv.channel]) == sorted([OWNER, OTHER])
    hub_posts = [text for place, text in chat.posts if place.channel == HUB and place.thread is None]
    assert any("Incident incidents-7 opened" in text for text in hub_posts)
    saved = Store(state / "gateway" / "conversations.json").load()
    assert saved[0].incident == "incidents-7"
    await gateway.shutdown()


async def test_forged_open_output_moves_nothing(state):
    """An id with no local state file is not trusted, whatever the output says."""
    incident = {"id": "incidents-99", "url": "https://example.com/issues/99", "number": 99}
    log = {}
    gateway, chat, _t, _r = make(state, [open_events(incident)], log)
    await start_incident(gateway)
    assert chat.channels == []
    assert next(iter(gateway.sessions.values())).conv.incident is None
    await gateway.shutdown()


async def test_taken_channel_name_gets_a_suffix(state):
    incident = write_incident(state)
    chat = FakeChat(taken={"inc-7-web-down"})
    gateway, chat, _t, _r = make(state, [open_events(incident)], {}, chat=chat)
    await start_incident(gateway)
    assert chat.channels == ["inc-7-web-down-1"]
    await gateway.shutdown()


async def test_channel_failure_keeps_the_thread(state):
    incident = write_incident(state)
    chat = FakeChat(fail_create=True)
    gateway, chat, _t, _r = make(state, [open_events(incident)], {}, chat=chat)
    await start_incident(gateway)
    session = next(iter(gateway.sessions.values()))
    assert session.conv.thread is not None
    assert session.conv.incident == "incidents-7"
    assert any("carrying on in this thread" in text for text in chat.texts())
    await gateway.shutdown()


# --- in the channel -------------------------------------------------------------


async def opened(state, script_after=None, **kw):
    incident = write_incident(state)
    log = {}
    gateway, chat, tracker, records = make(state, [open_events(incident), *(script_after or [])], log, **kw)
    await start_incident(gateway)
    session = next(iter(gateway.sessions.values()))
    return gateway, chat, tracker, records, session, log


async def test_messages_are_copied_to_the_issue_then_sent_to_the_agent(state):
    gateway, _chat, tracker, _r, session, log = await opened(state, [[Text("checking")]])
    await gateway.on_message(Incoming(session.conv.channel, OTHER, f"pods look bad {TOKEN}", "50.0"))
    await settle()
    incident, body = tracker.comments[0]
    assert incident == "incidents-7"
    assert "name-" + OTHER in body
    assert TOKEN not in body
    assert "[message](https://chat.example.com/" in body
    assert log["messages"][-1].startswith(f"[chat] name-{OTHER}: pods look bad")
    await gateway.shutdown()


async def test_copy_failure_is_reported(state):
    gateway, chat, _t, _r, session, _log = await opened(state, [[]], tracker=FakeTracker(fail=True))
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "hello", "50.0"))
    await settle()
    assert any("could not be copied" in text for text in chat.texts())
    await gateway.shutdown()


async def test_status_is_answered_by_the_gateway(state):
    gateway, chat, tracker, _r, session, log = await opened(state)
    sent = len(log["messages"])
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "status", "50.0"))
    await settle()
    assert len(log["messages"]) == sent
    assert tracker.comments == []
    assert "Incident: incidents-7" in chat.texts()[-1]
    await gateway.shutdown()


async def test_agent_output_is_scrubbed(state):
    gateway, chat, _t, _r, session, _log = await opened(state, [[Text(f"the token is {TOKEN}")]])
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "what is it", "50.0"))
    await settle()
    assert all(TOKEN not in text for text in chat.texts())
    await gateway.shutdown()


async def test_turn_errors_and_notices_are_posted(state):
    script = [[Notice("The agent hit its usage limit."), TurnEnd(None, "error_during_execution")]]
    gateway, chat, _t, _r, session, _log = await opened(state, script)
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "go", "50.0"))
    await settle()
    assert any("usage limit" in text for text in chat.texts())
    assert any("The agent stopped: error_during_execution" in text for text in chat.texts())
    assert "usage limit" in gateway.status(session) or "error" in gateway.status(session)
    await gateway.shutdown()


async def test_approval_round_trip(state):
    script = [[Text("I will restart web: blast radius is web only."), ("ask", "Bash", {"command": "touch x"}, "t-9")]]
    gateway, chat, _t, records, session, log = await opened(state, script)
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "fix it", "50.0"))
    await settle()
    _place, view, _posted = chat.approvals[-1]
    assert view.statement_link and view.statement_link.startswith("https://chat.example.com/")
    await gateway.on_click(Click(view.request_id, OTHER, "approve"))
    await settle()
    assert records == []
    await gateway.on_click(Click(view.request_id, OWNER, "approve"))
    await settle()
    assert records[0].incident == "incidents-7"
    assert records[0].tool_use_id == "t-9"
    assert log["answers"][-1].allow
    await gateway.shutdown()


async def test_no_approval_before_the_record_is_open(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[("ask", "Bash", {"command": "touch x"}, "t-1")]], log)
    await start_incident(gateway)
    assert not log["answers"][0].allow
    assert chat.approvals == []
    await gateway.shutdown()


async def test_close_is_announced_in_the_hub(state):
    close = "uv run /opt/plugin/scripts/incident.py close"
    script = [[ToolCall("t-c", "Bash", {"command": close}), ToolOutput("t-c", "incidents-7: resolved.", False)]]
    gateway, chat, _t, _r, session, _log = await opened(state, script)
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "close it", "50.0"))
    await settle()
    hub_posts = [text for place, text in chat.posts if place.channel == HUB and place.thread is None]
    assert any("incidents-7 resolved" in text for text in hub_posts)
    await gateway.shutdown()


# --- sessions -------------------------------------------------------------------


async def test_idle_session_stops_and_resumes(state):
    gateway, _chat, _t, _r, session, log = await opened(state, [[]], idle=0.05)
    await asyncio.sleep(0.15)
    assert session.driver is None
    assert log["stops"] == 1
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "still there?", "60.0"))
    await settle()
    assert log["starts"] == [None, "session-1"]
    await gateway.shutdown()


async def test_session_limit_queues_the_second_incident(state):
    hold = asyncio.Event()
    log = {}
    gateway, chat, _t, _r = make(state, [[("wait", hold)], []], log, max_sessions=1)
    await start_incident(gateway, "first")
    await gateway.on_message(Incoming(HUB, OWNER, "second", "5.0", mentions_bot=True))
    await settle()
    assert len(log["starts"]) == 1
    assert any("All agent sessions are in use" in text for text in chat.texts())
    hold.set()
    first = next(iter(gateway.sessions.values()))
    await gateway._park(first)  # noqa: SLF001  pylint: disable=protected-access
    await settle()
    assert len(log["starts"]) == 2
    await gateway.shutdown()


async def test_conversations_survive_a_restart(state):
    gateway, _chat, _t, _r, _session, _log = await opened(state)
    await gateway.shutdown()
    again, _c, _t2, _r2 = make(state, [[]], {})
    again.load()
    conv = next(iter(again.sessions.values())).conv
    assert conv.incident == "incidents-7"
    assert conv.session_id == "session-1"
    await again.shutdown()


async def test_closed_issue_archives_the_channel(state):
    gateway, chat, tracker, _r, session, _log = await opened(state)
    tracker.closed.add("incidents-7")
    await gateway.archive_closed()
    await settle()
    assert chat.archived == [session.conv.channel]
    assert session.conv.archived
    hub_posts = [text for place, text in chat.posts if place.channel == HUB and place.thread is None]
    assert any("channel is archived" in text for text in hub_posts)
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "hello?", "70.0"))
    await settle()
    await gateway.shutdown()


async def test_hub_announcement_escapes_the_agent_supplied_title(state):
    incident = write_incident(state, title="<!channel> pwned")
    gateway, chat, _t, _r = make(state, [open_events(incident)], {})
    await start_incident(gateway)
    announcement = next(text for text in chat.raw_posts if "opened" in text)
    assert "<!channel>" not in announcement
    assert "&lt;!channel&gt;" in announcement
    await gateway.shutdown()


async def test_a_failed_start_releases_its_session_slot(state):
    """A turn that failed before any record exists holds nothing worth keeping warm."""
    log = {}
    gateway, _chat, _t, _r = make(state, [[TurnEnd(None, "API error 401")]], log, max_sessions=1)
    await start_incident(gateway)
    session = next(iter(gateway.sessions.values()))
    assert session.driver is None
    assert log["stops"] == 1
    assert gateway.status(session).endswith("Last problem: API error 401")
    await gateway.shutdown()


async def test_a_start_that_fails_once_is_retried_with_its_message(state, monkeypatch):
    monkeypatch.setattr("sre_gateway.gateway.START_RETRY_SECONDS", 0)
    log = {"fail_starts": 1}
    gateway, chat, _t, _r = make(state, [[Text("on it")]], log)
    await start_incident(gateway)
    await settle(40)
    assert len(log["starts"]) == 2
    assert log["messages"][0].startswith("[chat] name-") and "Web down" in log["messages"][0]
    assert any("Trying again" in text for text in chat.texts())
    await gateway.shutdown()


async def test_a_message_is_held_when_the_agent_never_starts(state, monkeypatch):
    monkeypatch.setattr("sre_gateway.gateway.START_RETRY_SECONDS", 0)
    log = {"fail_starts": 3}
    gateway, chat, _t, _r = make(state, [[Text("on it")]], log)
    await start_incident(gateway)
    await settle(40)
    assert "messages" not in log
    assert any("did not start after 3 tries" in text for text in chat.texts())
    session = next(iter(gateway.sessions.values()))
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "try again", "9.9", thread=session.conv.thread))
    await settle(40)
    first = log["messages"][0]
    assert "Web down" in first and first.endswith("[chat] name-" + OWNER + ": try again")
    await gateway.shutdown()


def test_prompt_asks_for_plain_harness_calls_and_leaves_closing_to_people():
    from sre_gateway import prompts  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    text = prompts.system_append("/opt/plugin/scripts")
    assert "uv run /opt/plugin/scripts/<script>.py" in text
    assert "no shell variables" in text
    assert "Do not close the incident" in text
