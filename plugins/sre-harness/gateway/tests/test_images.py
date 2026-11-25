"""Images the agent shares with share.py: posted to its own conversation, recorded, never trusted blindly (ADR 0027)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fakes import FakeChat, settle
from sre_gateway import harness
from sre_gateway.agent import Text, ToolCall, ToolOutput
from sre_gateway.chat import Command
from test_gateway import HUB, OWNER, make, open_events, start_incident, write_incident  # pylint: disable=wrong-import-order

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 200
SHARE_CMD = "uv run /opt/plugin/scripts/share.py /tmp/load.png --title 'k8s03 load, day'"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path


def spool(tmp_path: Path, data: bytes = PNG, title: str = "k8s03 load, day") -> str:
    """What share.py does before the gateway sees its output; returns its result line."""
    source = tmp_path / "load.png"
    source.write_bytes(data)
    return json.dumps(harness.share.share(source, title, {"chat": {}}), sort_keys=True)


def share_events(output: str, call_id: str = "t-share", command: str = SHARE_CMD, error: bool = False) -> list:
    return [ToolCall(call_id, "Bash", {"command": command}), ToolOutput(call_id, output, error)]


def spool_files() -> list[Path]:
    directory = harness.share.spool_dir()
    return sorted(directory.iterdir()) if directory.is_dir() else []


async def test_an_incidents_image_is_posted_there_kept_and_described_on_the_issue(state):
    incident = write_incident(state)
    line = spool(state)
    digest = hashlib.sha256(PNG).hexdigest()
    log: dict = {}
    script = [[*open_events(incident), *share_events(line), Text("Load is climbing.")]]
    gateway, chat, tracker, _r = make(state, script, log, evidence_dir=state / "evidence")
    await start_incident(gateway)
    await settle(60)
    session = next(iter(gateway.sessions.values()))
    assert chat.uploads == [(session.place, f"{digest[:12]}.png", "k8s03 load, day", PNG)]
    assert (harness.share.incident_images_dir("incidents-7") / f"{digest}.png").read_bytes() == PNG
    assert (state / "evidence" / "incidents-7" / f"shared-{digest[:12]}.png").read_bytes() == PNG
    described = [body for incident_id, body in tracker.comments if "Image shared" in body]
    assert len(described) == 1
    assert "`k8s03 load, day`" in described[0]
    assert digest[:16] in described[0]
    assert spool_files() == []
    await gateway.shutdown()


async def test_an_image_before_the_record_is_posted_but_not_kept(state):
    line = spool(state)
    gateway, chat, tracker, _r = make(state, [share_events(line)], {})
    await start_incident(gateway)
    await settle(40)
    assert len(chat.uploads) == 1
    assert not (state / "xdg" / "sre-harness" / "images").exists()
    assert tracker.comments == []
    await gateway.shutdown()


async def test_output_naming_an_image_not_in_the_spool_posts_nothing(state):
    forged = json.dumps({"shared": "a" * 64, "type": "png", "bytes": 1, "title": "x"})
    gateway, chat, _t, _r = make(state, [share_events(forged)], {})
    await start_incident(gateway)
    await settle(40)
    assert chat.uploads == []
    await gateway.shutdown()


async def test_another_command_that_prints_a_share_line_posts_nothing(state):
    line = spool(state)
    gateway, chat, _t, _r = make(state, [share_events(line, command="cat /tmp/out.json")], {})
    await start_incident(gateway)
    await settle(40)
    assert chat.uploads == []
    assert spool_files() != []
    await gateway.shutdown()


async def test_a_failed_share_call_posts_nothing(state):
    line = spool(state)
    gateway, chat, _t, _r = make(state, [share_events(line, error=True)], {})
    await start_incident(gateway)
    await settle(40)
    assert chat.uploads == []
    await gateway.shutdown()


async def test_a_refused_upload_is_said_in_the_channel_and_nothing_is_kept(state):
    incident = write_incident(state)
    line = spool(state)
    chat = FakeChat(refuse_upload="Slack refused the upload: the app needs the files:write scope")
    gateway, chat, tracker, _r = make(
        state, [[*open_events(incident), *share_events(line)]], {}, chat=chat, evidence_dir=state / "evidence"
    )
    await start_incident(gateway)
    await settle(60)
    assert any("could not be posted" in text and "files:write" in text for text in chat.texts())
    assert not (state / "evidence" / "incidents-7").exists()
    assert not any("Image shared" in body for _i, body in tracker.comments)
    assert spool_files() == []
    session = next(iter(gateway.sessions.values()))
    assert "files:write" in (session.last_problem or "")
    await gateway.shutdown()


async def test_the_caption_is_scrubbed_before_it_leaves(state):
    token = "xoxb-" + "123456789012-abcdefghijkl"
    line = spool(state, title=f"load {token}")
    gateway, chat, _t, _r = make(state, [share_events(line)], {})
    await start_incident(gateway)
    await settle(40)
    assert token not in chat.uploads[0][2]
    await gateway.shutdown()


async def test_a_questions_image_is_posted_in_its_thread_and_kept_beside_its_log(state):
    line = spool(state)
    gateway, chat, _t, _r = make(state, [share_events(line)], {})
    await gateway.on_command(Command("sre", OWNER, "C" + "0ELSEWHE1", "how loaded is k8s03?"))
    await settle(40)
    assert len(chat.uploads) == 1
    assert chat.uploads[0][0].channel == HUB
    assert chat.uploads[0][0].thread is not None
    logs = sorted((state / "gateway" / "questions").glob("q-*.jsonl"))
    records = [json.loads(row) for row in logs[0].read_text().splitlines()]
    shared = [record for record in records if record["event"] == "shared"]
    digest = hashlib.sha256(PNG).hexdigest()
    assert shared and shared[0]["image"]["sha256"] == digest
    assert (logs[0].with_name(f"{logs[0].stem}.images") / f"{digest}.png").read_bytes() == PNG
    await gateway.shutdown()
