"""Reclaiming: an ended incident's or a question's idle agent never holds a slot against other work (homelabia#274)."""

from __future__ import annotations

import asyncio
import json

import pytest

from fakes import settle
from sre_gateway.agent import Text, ToolCall, ToolOutput
from sre_gateway.chat import Command, Incoming
from sre_gateway.gateway import QUESTION_BUSY, QUESTION_PREEMPTED, RECLAIMED
from test_gateway import HUB, OWNER, make, open_events, opened, write_incident  # pylint: disable=wrong-import-order

CLOSE = "uv run /opt/plugin/scripts/incident.py close"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


def close_turn() -> list:
    return [ToolCall("t-c", "Bash", {"command": CLOSE}), ToolOutput("t-c", "incidents-7: resolved.", False)]


async def start(gateway, text, ts):
    await gateway.on_message(Incoming(HUB, OWNER, text, ts, mentions_bot=True))
    await settle()


async def two_ended_incidents(state, script, **kw):
    """Two incident sessions whose agents are live and idle, both ended (closed), the first idle longest."""
    log = {}
    gateway, chat, _tracker, _records = make(state, [[], [], *script], log, **kw)
    await start(gateway, "web down", "5.0")
    await start(gateway, "dns down", "6.0")
    first, second = list(gateway.sessions.values())[:2]  # pylint: disable=unbalanced-tuple-unpacking
    first.conv.ended = second.conv.ended = True
    second.idle_since = first.idle_since + 1
    return gateway, chat, log, first, second


# --- questions ------------------------------------------------------------------


async def test_a_question_reclaims_an_ended_incident_and_the_incident_resumes_later(state):
    gateway, chat, log, first, second = await two_ended_incidents(state, [[Text("99.7%")], [Text("back")]])
    await gateway.on_command(Command("sre", OWNER, HUB, "where does the ingress SLO stand?"))
    await settle()
    assert QUESTION_BUSY not in [text for _user, text in chat.told]
    assert (first.place, RECLAIMED) in chat.posts and first.driver is None
    assert second.driver is not None
    assert "99.7%" in chat.texts()
    session_id = first.conv.session_id
    await gateway.on_message(Incoming(first.conv.channel, OWNER, "one more thing", "9.0", thread=first.conv.thread))
    await settle()
    assert log["starts"][-1] == session_id and first.driver is not None
    await gateway.shutdown()


async def test_a_question_does_not_reclaim_an_open_incident(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[], []], log)
    await start(gateway, "web down", "5.0")
    await start(gateway, "dns down", "6.0")
    first = next(iter(gateway.sessions.values()))
    first.conv.ended = True
    await gateway.on_command(Command("sre", OWNER, HUB, "how is ingress?"))
    await settle()
    assert chat.told[-1][1] == QUESTION_BUSY
    assert RECLAIMED not in chat.texts()
    await gateway.shutdown()


async def test_a_question_never_reclaims_another_question(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text("first answer")]], log)
    await gateway.on_command(Command("sre", OWNER, HUB, "first?"))
    await settle()
    await gateway.on_command(Command("sre", OWNER, HUB, "second?"))
    await settle()
    assert chat.told[-1][1] == QUESTION_BUSY
    assert QUESTION_PREEMPTED not in chat.texts()
    await gateway.shutdown()


async def test_the_question_room_ignores_an_ended_incident(state):
    gateway, chat, _log, first, second = await two_ended_incidents(state, [[Text("ok")]], max_sessions=3)
    assert first.driver is not None and second.driver is not None
    await gateway.on_command(Command("sre", OWNER, HUB, "how is ingress?"))
    await settle()
    # A slot was free: nothing needed reclaiming.
    assert RECLAIMED not in chat.texts() and "ok" in chat.texts()
    await gateway.shutdown()


# --- incident work ----------------------------------------------------------------


async def test_new_incident_work_reclaims_an_ended_incident_before_a_question(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[], [Text("answer")], []], log)
    await start(gateway, "web down", "5.0")
    ended = next(iter(gateway.sessions.values()))
    ended.conv.ended = True
    await gateway.on_command(Command("sre", OWNER, HUB, "how is ingress?"))
    await settle()
    question = next(s for s in gateway.sessions.values() if s.question)
    await start(gateway, "dns down", "7.0")
    assert (ended.place, RECLAIMED) in chat.posts and ended.driver is None
    assert question.driver is not None
    assert len(log["starts"]) == 3
    assert "All agent sessions are in use" not in " ".join(chat.texts())
    await gateway.shutdown()


async def test_a_busy_ended_incident_finishes_its_turn_then_gives_way(state):
    hold = asyncio.Event()
    log = {}
    gateway, chat, _t, _r = make(state, [[], [("wait", hold), Text("wrapping up")], []], log, max_sessions=1)
    await start(gateway, "web down", "5.0")
    ended = next(iter(gateway.sessions.values()))
    ended.conv.ended = True
    await gateway.on_message(Incoming(HUB, OWNER, "thanks all", "5.5", thread=ended.conv.thread))
    await settle()
    await start(gateway, "dns down", "7.0")
    assert len(log["starts"]) == 1 and ended.driver is not None
    hold.set()
    await settle(40)
    texts = [text for place, text in chat.posts if place == ended.place]
    assert texts[-2:] == ["wrapping up", RECLAIMED]
    assert len(log["starts"]) == 2
    await gateway.shutdown()


async def test_a_closed_incident_is_reclaimable_and_an_open_one_is_not(state):
    gateway, chat, _t, _r, session, _log = await opened(state, [close_turn()], max_sessions=1)
    assert not session.conv.ended
    assert gateway._reclaimable(for_question=False) == []  # pylint: disable=protected-access
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "close it", "50.0"))
    await settle()
    assert session.conv.ended and session.driver is not None
    assert gateway._reclaimable(for_question=True) == [session]  # pylint: disable=protected-access
    assert RECLAIMED not in chat.texts()
    await gateway.shutdown()


# --- un-ending --------------------------------------------------------------------


async def test_attaching_to_an_open_incident_un_ends_the_conversation(state):
    gateway, _chat, _t, _r, session, _log = await opened(state, [open_events(write_incident(state), "t-again")])
    session.conv.ended = True
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "reopen it", "50.0"))
    await settle()
    assert not session.conv.ended
    await gateway.shutdown()


async def test_attaching_to_a_resolved_incident_leaves_it_ended(state):
    incident = write_incident(state)
    gateway, _chat, _t, _r, session, _log = await opened(state, [open_events(incident, "t-again")])
    path = state / "xdg" / "sre-harness" / "incidents" / f"{incident['id']}.json"
    path.write_text(json.dumps({**incident, "status": "resolved"}), encoding="utf-8")
    session.conv.ended = True
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "attach it", "50.0"))
    await settle()
    assert session.conv.ended
    await gateway.shutdown()
