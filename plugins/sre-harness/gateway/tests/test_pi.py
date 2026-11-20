"""The Pi driver, against a stand-in Pi that speaks the RPC protocol."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from sre_gateway import harness, pi
from sre_gateway.__main__ import pi_problems
from sre_gateway.agent import Notice, PermissionAnswer, Text, ToolCall, ToolOutput, TurnEnd
from sre_gateway.config import Config

FAKE = Path(__file__).with_name("fake_pi.py")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    launcher = tmp_path / "pi"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setenv("FAKE_PI_LOG", str(tmp_path / "starts.jsonl"))
    return pi.PiSetup(
        pi=str(launcher),
        model="claude-bridge/claude-opus-5-5",
        home=tmp_path / "pi-home",
        session_dir=tmp_path / "sessions",
        cwd=site,
    )


def starts(setup):
    return [json.loads(line) for line in (setup.cwd.parent / "starts.jsonl").read_text(encoding="utf-8").splitlines()]


def policy(triage=False):
    rules = harness.chat_policy.triage_rules({}) if triage else harness.chat_policy.rules({})
    return harness.tool_policy.Policy.from_rules(rules)


class Asker:
    """The gateway's approval callback: records each request and answers as told."""

    def __init__(self, allow=False):
        self.allow = allow
        self.asked = []

    async def __call__(self, tool, tool_input, call_id):
        self.asked.append((tool, tool_input, call_id))
        return PermissionAnswer(self.allow, "approved" if self.allow else "an approver said no")


async def run(driver, message):
    return [event async for event in driver.turn(message)]


async def started(setup, ask=None, triage=None, resume=None):
    driver = pi.PiDriver(setup, policy(bool(triage)), ask or Asker(), system_append="extra", triage_token=triage)
    await driver.start(resume)
    return driver


async def test_a_reply_becomes_text_and_a_turn_end(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "hello")
    finally:
        await driver.stop()
    assert events == [Text("Hello from Pi."), TurnEnd(driver.session_id)]


async def test_pi_runs_isolated_with_only_the_harness(setup):
    driver = await started(setup)
    await driver.stop()
    (entry,) = starts(setup)
    argv = entry["argv"]
    assert entry["home"] == str(setup.home)
    assert entry["cwd"] == str(setup.cwd)
    assert entry["triage"] is None
    for flag in ("--no-skills", "--no-context-files", "--no-prompt-templates", "--no-approve"):
        assert flag in argv
    extensions = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--extension"]
    assert [Path(e).name for e in extensions] == ["sre-audit.ts", "sre-policy.ts"]
    assert argv[argv.index("--tools") + 1] == "read,bash,grep,find,ls,edit,write"
    assert "codemode" in argv[argv.index("--exclude-tools") + 1]
    assert argv[argv.index("--model") + 1] == "claude-bridge/claude-opus-5-5"


async def test_an_allowed_call_runs_without_asking(setup):
    ask = Asker()
    driver = await started(setup, ask)
    try:
        events = await run(driver, 'tool bash {"command": "date"}')
    finally:
        await driver.stop()
    assert ask.asked == []
    assert ToolCall("call-1", "Bash", {"command": "date"}) in events
    assert ToolOutput("call-1", "output of bash", False) in events
    assert Text("ran") in events


async def test_a_denied_call_is_blocked_without_asking(setup):
    ask = Asker(allow=True)
    driver = await started(setup, ask)
    try:
        events = await run(driver, 'tool bash {"command": "printenv"}')
    finally:
        await driver.stop()
    assert ask.asked == []
    assert any(isinstance(e, Text) and e.text.startswith("blocked: denied by Bash(printenv") for e in events)


async def test_anything_else_goes_to_an_approver_bound_to_the_call(setup):
    ask = Asker(allow=True)
    driver = await started(setup, ask)
    try:
        events = await run(driver, 'tool bash {"command": "systemctl restart nginx"}')
    finally:
        await driver.stop()
    assert ask.asked == [("Bash", {"command": "systemctl restart nginx"}, "call-1")]
    assert Text("ran") in events


async def test_an_approver_refusal_blocks_the_call(setup):
    driver = await started(setup, Asker(allow=False))
    try:
        events = await run(driver, 'tool mcp__nagios__acknowledge_problem {"host": "web"}')
    finally:
        await driver.stop()
    assert Text("blocked: an approver said no") in events
    assert not any(isinstance(e, ToolCall) for e in events)


async def test_triage_carries_its_token_and_refuses_closing(setup):
    ask = Asker(allow=True)
    driver = await started(setup, ask, triage="tok-1")
    scripts = harness.SCRIPTS
    try:
        events = await run(driver, "tool bash " + json.dumps({"command": f"uv run {scripts}/incident.py close"}))
    finally:
        await driver.stop()
    assert starts(setup)[0]["triage"] == "tok-1"
    assert ask.asked == []
    assert any(isinstance(e, Text) and e.text.startswith("blocked: denied") for e in events)


async def test_a_question_reads_but_cannot_write_its_timeline(setup):
    ask = Asker(allow=True)
    rules = harness.tool_policy.Policy.from_rules(harness.chat_policy.question_rules({}))
    driver = pi.PiDriver(setup, rules, ask, system_append="extra")
    await driver.start(None)
    scripts = harness.SCRIPTS
    try:
        read = await run(driver, "tool bash " + json.dumps({"command": f"uv run {scripts}/slo.py status"}))
        write = await run(driver, "tool bash " + json.dumps({"command": f"uv run {scripts}/timeline.py add x"}))
    finally:
        await driver.stop()
    assert Text("ran") in read
    assert any(isinstance(e, Text) and e.text.startswith("blocked: denied") for e in write)
    assert ask.asked == []


async def test_concurrent_dialogs_are_each_answered(setup):
    driver = await started(setup, Asker(allow=False))
    try:
        events = await run(driver, "two tools")
    finally:
        await driver.stop()
    assert Text("ran / blocked: an approver said no") in events


async def test_other_dialogs_are_declined(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "confirm")
    finally:
        await driver.stop()
    assert Text("cancelled") in events


async def test_a_provider_error_is_a_notice_and_a_failed_turn(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "error")
    finally:
        await driver.stop()
    assert any(isinstance(e, Notice) and "usage limit" in e.text for e in events)
    assert isinstance(events[-1], TurnEnd)
    assert "429" in (events[-1].error or "")


async def test_malformed_lines_are_skipped(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "garbage")
    finally:
        await driver.stop()
    assert events == [Text("still here"), TurnEnd(driver.session_id)]


async def test_a_crash_mid_turn_ends_the_turn_with_an_error(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "crash")
    finally:
        await driver.stop()
    assert isinstance(events[-1], TurnEnd)
    assert events[-1].error


async def test_resume_reopens_the_same_session(setup):
    first = await started(setup)
    session = first.session_id
    await first.stop()
    again = await started(setup, resume=session)
    await again.stop()
    assert again.session_id == session
    assert starts(setup)[1]["argv"][-2:] == ["--session", session]


async def test_a_chat_message_is_never_a_pi_command(setup):
    driver = await started(setup)
    try:
        events = await run(driver, "/reload")
    finally:
        await driver.stop()
    assert events[0] == Text("echo: (chat message) /reload")


def test_the_pi_home_gets_direct_mcp_and_a_private_nagios_login(tmp_path):
    home = tmp_path / "pi"
    servers = {
        "grafana": {"command": "grafana-mcp", "args": ["-t", "stdio"]},
        "wazuh": {"type": "sse", "url": "http://x/sse"},
        "nagios": {"type": "sse", "url": "http://y/sse"},
    }
    login = {"nagios_url": "http://127.0.0.1:8080/", "nagios_user": "readonly", "nagios_pass": "pw"}
    warnings = pi.prepare_home(home, servers, login, "/usr/bin/nagios-mcp")
    config = json.loads((home / "mcp.json").read_text(encoding="utf-8"))
    assert config["autoEnableCodemode"] is False
    assert set(config["mcpServers"]) == {"grafana", "nagios"}
    assert all(s["exposure"] == "direct" for s in config["mcpServers"].values())
    assert "pw" not in (home / "mcp.json").read_text(encoding="utf-8")
    secret = home / "nagios-mcp.json"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert any("wazuh" in w for w in warnings)


def test_check_refuses_pi_without_a_model_a_binary_or_the_readonly_login(tmp_path):
    runner = {"status_url": "http://127.0.0.1:8080/cgi-bin/statusjson.cgi"}
    config = Config(tmp_path, {"runner": runner}, {}, {}, agent="pi", model="opus", pi=str(tmp_path / "missing"))
    problems = " | ".join(pi_problems(config))
    assert "provider/id" in problems
    assert "no Pi binary" in problems
    assert "read-only Nagios login" in problems
    assert config.nagios_base_url() == "http://127.0.0.1:8080/"
    ready = Config(
        tmp_path,
        {"runner": runner},
        {},
        {},
        runner_auth=("readonly", "pw"),
        agent="pi",
        model="claude-bridge/claude-opus-5-5",
        pi=sys.executable,
    )
    assert not pi_problems(ready)
