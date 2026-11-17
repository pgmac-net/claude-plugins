"""Unattended triage in the gateway: alerts start triage-only sessions; the first responder ends triage-only."""

from __future__ import annotations

import asyncio
import json

import pytest

from fakes import settle
from sre_gateway import prompts
from sre_gateway.agent import Text, ToolCall, ToolOutput
from sre_gateway.chat import Command, Incoming
from sre_gateway.runner import Alert, Runner, RunnerSettings, RunnerState
from test_gateway import HUB, OWNER, STRANGER, write_incident  # pylint: disable=wrong-import-order
from test_gateway import make as make_gateway  # pylint: disable=wrong-import-order

NOW = 1_800_000_000
OPEN_AUTO = "uv run /opt/plugin/scripts/incident.py open --auto --title 'Shop down' --severity P2"


class FakeFeed:
    def __init__(self, problems=None, index=None):
        self.result = list(problems or [])
        self.fail = False
        self._index = index or {"web01": [("component:shop", 1)], "lab01": [("component:toy", 3)]}

    async def problems(self):
        if self.fail:
            raise ConnectionError("no route")
        return list(self.result)

    def index(self):
        return self._index


def alert(check="HTTP", host="web01", since=NOW - 600) -> Alert:
    return Alert(host, check, "CRITICAL", "connection refused", since)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


def make(state, script, log, feed, max_auto=1):
    runner = Runner(
        RunnerSettings(
            enabled=True,
            tiers=[1],
            poll_seconds=60,
            max_auto_sessions=max_auto,
            cooldown_minutes=30,
            min_state_age_seconds=0,
        ),
        feed,
        RunnerState(state / "gateway" / "runner.json"),
        clock=lambda: NOW,
    )
    policies: list = []
    gateway, chat, _tracker, records = make_gateway(
        state, script, log, max_sessions=3, runner=runner, record_policy=policies.append
    )
    return gateway, chat, records, policies


def only(items):
    """The single item of a collection, failing clearly if there is not exactly one."""
    items = list(items)
    assert len(items) == 1, items
    return items[0]


def opened_auto(incident: dict) -> list:
    out = json.dumps({"id": incident["id"], "url": incident["url"], "bind": "0" * 32})
    return [ToolCall("t-open", "Bash", {"command": OPEN_AUTO}), ToolOutput("t-open", out, False)]


async def test_a_new_alert_starts_a_triage_only_session(state):
    log: dict = {}
    gateway, chat, _records, _policies = make(state, [[Text("triaging")]], log, FakeFeed([alert()]))
    await gateway.poll_once()
    await settle()
    assert "Unattended triage started for:" in chat.texts()[0]
    assert "web01/HTTP is CRITICAL [component:shop]" in chat.texts()[0]
    session = only(gateway.sessions.values())
    assert session.conv.auto and session.conv.policy == "triage-only"
    assert len(session.conv.triage_token) == 32
    assert log["policies"] == ["triage-only"]
    assert log["messages"][0].startswith("[alert]") and "web01/HTTP" in log["messages"][0]


async def test_the_same_alert_starts_nothing_twice(state):
    log: dict = {}
    feed = FakeFeed([alert()])
    gateway, _chat, _records, _policies = make(state, [[]], log, feed)
    await gateway.poll_once()
    await settle()
    await gateway.poll_once()
    await settle()
    assert len(gateway.sessions) == 1
    assert json.loads((state / "gateway" / "runner.json").read_text())["cooldowns"]["component:shop"] == NOW


async def test_a_hobby_tier_alert_starts_nothing(state):
    gateway, chat, _records, _policies = make(state, [], {}, FakeFeed([alert("Disk", host="lab01")]))
    await gateway.poll_once()
    await settle()
    assert gateway.sessions == {} and chat.posts == []


async def test_triage_refuses_without_asking_and_records_it(state):
    incident = write_incident(state, number=12)
    incident["id"] = "catalog-12"
    (state / "xdg" / "sre-harness" / "incidents" / "catalog-12.json").write_text(json.dumps(incident))
    script = [[*opened_auto(incident), ("ask", "Bash", {"command": "kubectl delete pod shop-1"}, "t-1")]]
    log: dict = {}
    gateway, chat, records, _policies = make(state, script, log, FakeFeed([alert()]))
    await gateway.poll_once()
    await settle(60)
    assert chat.approvals == []
    answer = only(log["answers"])
    assert answer.allow is False and "unattended triage" in answer.message
    decision = only(records)
    assert (decision.incident, decision.outcome, decision.approver) == ("catalog-12", "refused", None)


async def test_first_responder_message_ends_triage_only_for_good(state):
    incident = write_incident(state, number=12)
    log: dict = {}
    gateway, chat, _records, policies = make(
        state, [opened_auto(incident), [Text("ok")], [Text("again")]], log, FakeFeed([alert()])
    )
    await gateway.poll_once()
    await settle(60)
    session = only(gateway.sessions.values())
    channel = session.conv.channel
    await gateway.on_message(Incoming(channel, STRANGER, "do it", "9.1"))
    await settle(30)
    assert session.conv.policy == "triage-only"
    await gateway.on_message(Incoming(channel, OWNER, "looking now", "9.2"))
    await settle(60)
    assert session.conv.policy == "chat"
    change = only(policies)
    assert change.incident == incident["id"] and (change.old, change.new) == ("triage-only", "chat")
    assert OWNER in change.by
    assert log["policies"] == ["triage-only", "chat"]
    assert log["messages"][-1].startswith("[gateway] name-") and "looking now" in log["messages"][-1]
    assert any("no longer triage-only" in text for text in chat.texts())
    await gateway.on_message(Incoming(channel, OWNER, "and again", "9.3"))
    await settle(60)
    assert len(policies) == 1 and log["messages"][-1] == "[chat] name-" + OWNER + ": and again"


async def test_alerts_go_into_an_open_incident_instead_of_a_new_one(state):
    log: dict = {}
    feed = FakeFeed([alert()])
    gateway, chat, _records, _policies = make(state, [[], []], log, feed)
    await gateway.poll_once()
    await settle()
    feed.result = [alert("Ping", since=NOW - 300)]
    await gateway.poll_once()
    await settle()
    assert len(gateway.sessions) == 1
    assert any("New alerts while this incident is open" in text for text in chat.texts())
    assert log["messages"][-1].startswith("[alert] While this incident is open")
    assert "web01/Ping is CRITICAL [component:shop]" in log["messages"][-1]


async def test_a_drill_never_holds_back_a_real_triage(state):
    gateway, chat, _records, _policies = make(state, [[], []], {}, FakeFeed([alert()]))
    await gateway.on_command(Command("incident", OWNER, HUB, "drill alert web01/HTTP pretend it broke"))
    await settle()
    await gateway.poll_once()
    await settle()
    kinds = sorted(s.conv.drill for s in gateway.sessions.values())
    assert kinds == [False, True]
    assert not any("capacity" in text.lower() for text in chat.texts())


async def test_a_dismissed_triage_takes_no_more_alerts(state):
    private = write_incident(state, number=12)
    dismiss = "uv run /opt/plugin/scripts/incident.py dismiss --by Owner --reason flap"
    script = [
        [*opened_auto(private), ToolCall("t-d", "Bash", {"command": dismiss}), ToolOutput("t-d", "dismissed", False)],
        [],
    ]
    feed = FakeFeed([alert()], index={"web01": [("component:shop", 1)], "web02": [("component:web", 1)]})
    gateway, _chat, _records, _policies = make(state, script, {}, feed)
    await gateway.poll_once()
    await settle(60)
    first = only(gateway.sessions.values())
    assert first.conv.ended and not first.conv.archived
    feed.result = [alert("Ping", host="web02", since=NOW - 300)]
    await gateway.poll_once()
    await settle(60)
    assert len(gateway.sessions) == 2


async def test_closed_incidents_are_archived_soon_after_start(state, monkeypatch):
    monkeypatch.setattr("sre_gateway.gateway.ARCHIVE_FIRST_SECONDS", 0)
    gateway, chat, _records, _policies = make(state, [[]], {}, FakeFeed())
    write_incident(state, number=12)
    await gateway.on_command(Command("incident", OWNER, HUB, "drill alert web01/HTTP pretend it broke"))
    await settle()
    session = only(gateway.sessions.values())
    session.conv.incident, session.conv.thread = "incidents-12", None
    gateway.tracker.closed.add("incidents-12")
    task = asyncio.create_task(gateway._archive_loop())  # pylint: disable=protected-access
    await settle(30)
    task.cancel()
    assert session.conv.archived and chat.archived


async def test_drill_alert_command_starts_a_drill_triage(state):
    log: dict = {}
    gateway, chat, _records, _policies = make(state, [[]], log, FakeFeed())
    await gateway.on_command(Command("incident", OWNER, HUB, "drill alert web01/HTTP pretend it broke"))
    await settle()
    session = only(gateway.sessions.values())
    assert session.conv.drill and session.conv.auto and session.conv.policy == "triage-only"
    assert "Drill: unattended triage started" in chat.texts()[0]
    assert "--auto --drill" in log["messages"][0] and "pretend it broke" in log["messages"][0]


async def test_source_failure_is_announced_once_and_recovery_too(state):
    feed = FakeFeed()
    feed.fail = True
    gateway, chat, _records, _policies = make(state, [], {}, feed)
    await gateway.poll_once()
    await gateway.poll_once()
    feed.fail = False
    await gateway.poll_once()
    texts = chat.texts()
    assert sum("could not be read" in t for t in texts) == 1
    assert sum("readable again" in t for t in texts) == 1


async def test_promote_moves_the_channel_to_the_public_incident(state):
    private = write_incident(state, number=12)
    public = write_incident(state, number=40, title="Shop down")
    promote_cmd = "uv run /opt/plugin/scripts/incident.py promote --by Owner"
    out = json.dumps({"id": public["id"], "url": public["url"], "bind": "1" * 32})
    script = [opened_auto(private), [ToolCall("t-p", "Bash", {"command": promote_cmd}), ToolOutput("t-p", out, False)]]
    gateway, chat, _records, _policies = make(state, script, {}, FakeFeed([alert()]))
    await gateway.poll_once()
    await settle(60)
    session = only(gateway.sessions.values())
    await gateway.on_message(Incoming(session.conv.channel, OWNER, "it's real, promote it", "9.2"))
    await settle(60)
    assert session.conv.incident == public["id"]
    assert any(f"promoted to incident {public['id']}" in text for text in chat.texts())


def test_triage_prompt_is_only_for_triage():
    assert "UNATTENDED TRIAGE" in prompts.system_append("/s", triage=True)
    assert "UNATTENDED TRIAGE" not in prompts.system_append("/s")
    assert "open --auto" in prompts.system_append("/s", triage=True)
