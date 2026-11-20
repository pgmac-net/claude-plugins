"""Questions asked with /sre: read-only, no incident, yield to incident work, logged by the gateway."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from fakes import settle
from sre_gateway import questions
from sre_gateway.agent import Text, ToolCall, ToolOutput
from sre_gateway.chat import Command, Incoming, Place
from sre_gateway.gateway import (
    QUESTION_BUSY,
    QUESTION_ENDED,
    QUESTION_PREEMPTED,
    QUESTION_REFUSAL,
    QUESTION_TURNS,
)
from sre_gateway.store import Store
from test_gateway import HUB, OTHER, OWNER, STRANGER, TOKEN  # pylint: disable=wrong-import-order
from test_gateway import make as make_gateway  # pylint: disable=wrong-import-order

QUESTION = "where does the ingress SLO stand?"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


def make(state, script, log, max_sessions=2, question_idle=600):
    return make_gateway(
        state, script, log, max_sessions=max_sessions, question_idle=question_idle, env_values={TOKEN: "bot_token"}
    )


async def ask(gateway, text=QUESTION, user=OWNER):
    await gateway.on_command(Command("sre", user, "C" + "0ELSEWHE1", text))
    await settle()


def question_logs(state: Path) -> list[Path]:
    return sorted((state / "gateway" / "questions").glob("q-*.jsonl"))


def records_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- starting -------------------------------------------------------------------


async def test_sre_answers_in_a_hub_thread_and_opens_no_record(state):
    log = {}
    gateway, chat, tracker, records = make(state, [[Text("Ingress: 99.7% of 99.5%, budget healthy.")]], log)
    await ask(gateway)
    place, text = chat.posts[0]
    assert place == Place(HUB) and text == f"name-{OWNER} asked: {QUESTION}"
    session = next(iter(gateway.sessions.values()))
    assert session.question and session.place.channel == HUB and session.place.thread
    answer_place, answer = chat.posts[1]
    assert answer_place == session.place and "99.7%" in answer
    assert log["policies"] == ["question"]
    assert QUESTION in log["messages"][0] and "open no record" in log["messages"][0]
    assert not tracker.comments and not records and not chat.channels
    # Never written to the conversation store: a question does not survive a restart.
    assert Store(state / "gateway" / "conversations.json").load() == []
    (path,) = question_logs(state)
    first = records_of(path)[0]
    assert first["event"] == "asked" and first["user"] == OWNER and first["text"] == QUESTION
    assert first["capture"] == "gateway"
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    await gateway.shutdown()
    assert records_of(path)[-1] == {**records_of(path)[-1], "event": "ended", "reason": "the gateway stopped"}


async def test_a_non_responder_cannot_ask_and_an_empty_question_gets_usage(state):
    gateway, chat, _t, _r = make(state, [], {})
    await ask(gateway, user=STRANGER)
    await ask(gateway, text="   ")
    assert gateway.sessions == {} and not chat.posts
    assert [text for _user, text in chat.told] == [
        "Only listed responders can ask the agent.",
        "Usage: /sre <question>",
    ]
    assert question_logs(state) == []


async def test_a_hub_mention_still_starts_an_incident(state):
    log = {}
    gateway, _c, _t, _r = make(state, [[]], log)
    await gateway.on_message(Incoming(HUB, OWNER, "is ingress down?", "5.0", mentions_bot=True))
    await settle()
    assert log["policies"] == ["chat"]
    assert "start-incident skill" in log["messages"][0]
    await gateway.shutdown()


# --- the policy -----------------------------------------------------------------


async def test_a_call_outside_the_allow_list_is_refused_with_no_approval_request(state):
    log = {}
    restart = {"command": "kubectl rollout restart deploy/canary -n web"}
    gateway, chat, _t, records = make(state, [[("ask", "Bash", restart, "t-1"), Text("I cannot restart it.")]], log)
    await ask(gateway, "restart the canary please")
    (answer,) = log["answers"]
    assert not answer.allow and answer.message == QUESTION_REFUSAL
    assert chat.approvals == [] and records == []
    refused = [r for r in records_of(question_logs(state)[0]) if r["event"] == "refused"]
    assert refused == [{**refused[0], "tool": "Bash", "input": restart, "tool_use_id": "t-1"}]
    await gateway.shutdown()


async def test_tool_calls_are_logged_redacted_hashed_and_chained(state):
    log = {}
    call = ToolCall("t-1", "Bash", {"command": f"uv run /p/scripts/slo.py status --token {TOKEN}"})
    output = ToolOutput("t-1", "ingress 99.7%", False)
    gateway, _chat, _t, _r = make(state, [[call, output, Text("99.7%")]], log)
    await ask(gateway)
    (path,) = question_logs(state)
    events = records_of(path)
    assert [r["event"] for r in events] == ["asked", "attempt", "complete"]
    assert TOKEN not in path.read_text(encoding="utf-8")
    assert "slo.py status" in events[1]["input"]["command"]
    assert events[2]["output"]["bytes"] == len("ingress 99.7%") and "ingress" not in json.dumps(events[2])
    assert questions.verify(path)
    await gateway.shutdown()


async def test_an_incident_call_in_a_question_opens_nothing(state):
    log = {}
    out = json.dumps({"id": "incidents-7", "url": "https://example.com/issues/7", "bind": "0" * 32})
    events = [
        ToolCall("t-1", "Bash", {"command": "uv run /p/scripts/incident.py open --title x"}),
        ToolOutput("t-1", out, False),
    ]
    gateway, chat, _t, _r = make(state, [events], log)
    await ask(gateway)
    session = next(iter(gateway.sessions.values()))
    assert session.conv.incident is None and not chat.channels
    await gateway.shutdown()


async def test_alerts_never_go_into_a_question(state):
    gateway, _c, _t, _r = make(state, [[]], {})
    await ask(gateway)
    assert gateway._open_sessions(drill=False) == []  # pylint: disable=protected-access
    await gateway.shutdown()


async def test_a_question_whose_first_record_cannot_be_written_is_not_asked(state):
    (state / "gateway").mkdir(parents=True)
    (state / "gateway" / "questions").write_text("not a directory", encoding="utf-8")
    gateway, chat, _t, _r = make(state, [], {})
    await ask(gateway)
    assert gateway.sessions == {} and not chat.posts
    assert chat.told[-1][1] == "The question could not be recorded, so it was not asked."


# --- sessions and slots ---------------------------------------------------------


async def test_a_question_never_takes_the_last_session(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[], []], log)
    await gateway.on_command(Command("incident", OWNER, HUB, "Web down"))
    await settle()
    await ask(gateway)
    assert chat.told[-1][1] == QUESTION_BUSY
    assert [s for s in gateway.sessions.values() if s.question] == []
    await gateway.shutdown()


async def test_with_one_session_there_are_no_questions(state):
    gateway, chat, _t, _r = make(state, [], {}, max_sessions=1)
    await ask(gateway)
    assert chat.told[-1][1] == QUESTION_BUSY and gateway.sessions == {}


async def test_a_follow_up_in_the_thread_reaches_the_question(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text("99.7%")], [Text("99.1% last week")]], log)
    await ask(gateway)
    session = next(iter(gateway.sessions.values()))
    await gateway.on_message(Incoming(HUB, OTHER, "and last week?", "9.0", thread=session.place.thread))
    await settle()
    assert log["messages"][1] == f"[chat] name-{OTHER}: and last week?"
    assert "99.1% last week" in chat.texts()[-1]
    assert [r["event"] for r in records_of(question_logs(state)[0])][-1] == "message"
    await gateway.on_message(Incoming(HUB, OTHER, "status", "9.1", thread=session.place.thread))
    assert chat.texts()[-1].startswith("Question: read-only, no incident; turns used 2 of")
    await gateway.shutdown()


async def test_an_idle_question_ends_and_its_thread_points_to_sre(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text("99.7%")]], log, question_idle=0.05)
    await ask(gateway)
    thread = next(iter(gateway.sessions.values())).place.thread
    await asyncio.sleep(0.1)
    await settle()
    assert gateway.sessions == {}
    assert "ended after 1 minute(s) with no message" in chat.texts()[-1]
    assert log["stops"] == 1
    await gateway.on_message(Incoming(HUB, OTHER, "and last week?", "9.0", thread=thread))
    assert chat.told[-1] == (OTHER, QUESTION_ENDED)
    assert len(log["messages"]) == 1
    assert records_of(question_logs(state)[0])[-1]["reason"] == "idle"
    # Its slot is free again.
    assert gateway._live == 0  # pylint: disable=protected-access


async def test_a_question_ends_at_its_turn_limit(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text(f"answer {n}")] for n in range(QUESTION_TURNS + 1)], log)
    await ask(gateway)
    thread = next(iter(gateway.sessions.values())).place.thread
    for n in range(QUESTION_TURNS - 1):
        await gateway.on_message(Incoming(HUB, OWNER, f"more {n}", f"9.{n}", thread=thread))
        await settle()
    assert gateway.sessions == {}
    assert len(log["messages"]) == QUESTION_TURNS
    assert f"limit of {QUESTION_TURNS} turns" in chat.texts()[-1]


async def test_an_idle_question_gives_way_to_incident_work(state):
    log = {}
    gateway, chat, _t, _r = make(state, [[Text("99.7%")], [], []], log)
    await ask(gateway)
    question_place = next(iter(gateway.sessions.values())).place
    await gateway.on_command(Command("incident", OWNER, HUB, "Web down"))
    await settle()
    assert len(log["starts"]) == 2
    await gateway.on_command(Command("incident", OWNER, HUB, "DNS down"))
    await settle()
    assert len(log["starts"]) == 3
    assert (question_place, QUESTION_PREEMPTED) in chat.posts
    assert not any(s.question for s in gateway.sessions.values())
    assert "All agent sessions are in use" not in " ".join(chat.texts())
    assert records_of(question_logs(state)[0])[-1]["reason"] == "preempted"
    await gateway.shutdown()


async def test_a_busy_question_finishes_its_turn_then_gives_way(state):
    log = {}
    release = asyncio.Event()
    gateway, chat, _t, _r = make(state, [[("wait", release), Text("99.7%")], [], []], log)
    await ask(gateway)
    question_place = next(iter(gateway.sessions.values())).place
    await gateway.on_command(Command("incident", OWNER, HUB, "Web down"))
    await settle()
    await gateway.on_command(Command("incident", OWNER, HUB, "DNS down"))
    await settle()
    assert len(log["starts"]) == 2  # the second incident waits for the question's turn
    release.set()
    await settle(40)
    texts = [text for place, text in chat.posts if place == question_place]
    assert texts[-2:] == ["99.7%", QUESTION_PREEMPTED]
    assert len(log["starts"]) == 3
    await gateway.shutdown()


# --- the log --------------------------------------------------------------------


def test_prune_removes_only_old_question_logs(tmp_path):
    directory = tmp_path / "questions"
    directory.mkdir()
    old, new, other = directory / "q-old.jsonl", directory / "q-new.jsonl", directory / "notes.txt"
    for path in (old, new, other):
        path.write_text("{}\n", encoding="utf-8")
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    os.utime(other, (month_ago, month_ago))
    assert questions.prune(directory) == 1
    assert sorted(p.name for p in directory.iterdir()) == ["notes.txt", "q-new.jsonl"]
    assert questions.prune(tmp_path / "missing") == 0


def test_verify_catches_an_edited_line(tmp_path):
    qlog = questions.QuestionLog(tmp_path, "q-test")
    qlog.asked(OWNER, "how is ingress?")
    qlog.ended("idle")
    assert questions.verify(qlog.path)
    lines = qlog.path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("ingress", "egress")
    qlog.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not questions.verify(qlog.path)
