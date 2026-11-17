"""Other agents: records name the agent and model; Pi's adapter payloads bind and pair like Claude Code's."""

from __future__ import annotations

import json

import audit
import audit_hook
import pytest
import sre_common
from test_audit import call, log, open_incident

PI = {"agent": "pi", "model": "opencode-go/kimi-k3"}


def pi_call(event: str, tool_input: dict, response: str | None = None, call_id: str = "c1") -> dict:
    """What adapters/pi/sre-audit.ts sends for one Pi bash call."""
    payload = {
        "session_id": "pi-abc",
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": tool_input,
        "tool_use_id": call_id,
        **PI,
    }
    if response is not None:
        payload["tool_response"] = {"stdout": response, "is_error": False}
    return payload


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.mark.usefixtures("state")
def test_claude_code_records_name_the_default_agent():
    open_incident()
    audit_hook.handle(call("PreToolUse", tool_input={"command": "uptime"}))
    record = log()[-1]
    assert record["agent"] == "claude-code" and "model" not in record


@pytest.mark.usefixtures("state")
def test_a_pi_session_binds_from_the_open_output_and_pairs_its_calls():
    token = "ab" * 16
    binds = audit_hook.state_dir() / "binds"
    binds.mkdir(parents=True)
    (binds / token).write_text("catalog-9\n", encoding="utf-8")
    opened = json.dumps({"id": "catalog-9", "url": "https://example.com/9", "bind": token})
    command = {"command": "uv run /p/scripts/incident.py open --drill --title T --severity P3"}
    audit_hook.handle(pi_call("PreToolUse", command))
    audit_hook.handle(pi_call("PostToolUse", command, response=opened))
    audit_hook.handle(pi_call("PreToolUse", {"command": "uptime"}, call_id="c2"))
    audit_hook.handle(pi_call("PostToolUse", {"command": "uptime"}, response="up 3 days", call_id="c2"))
    records = log("catalog-9")
    assert [r["event"] for r in records] == ["complete", "attempt", "complete"]
    assert {(r["agent"], r["model"], r["capture"]) for r in records} == {("pi", "opencode-go/kimi-k3", "hook")}
    summary = audit.verify(audit.read_log("catalog-9"))
    assert summary["chain_intact"] and summary["attempts_without_completion"] == []
    assert summary["agents"] == {"pi": 3}


@pytest.mark.parametrize(
    ("agent", "model", "expected"),
    [
        ("pi", "llama/gpt-oss-20b", ("pi", "llama/gpt-oss-20b")),
        ("Pi Agent!", None, ("unknown", None)),
        ("pi", "x" * 300, ("pi", None)),
        (7, "m", ("unknown", "m")),
    ],
)
def test_agent_and_model_are_shape_checked(agent, model, expected):
    assert audit_hook.agent_of({"agent": agent, "model": model}) == expected


def test_a_self_reporting_agent_names_itself(state, site, monkeypatch):
    monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
    sre_common.write_incident_state({"id": "catalog-5", "repo": "o/r", "number": 5, "url": "u", "title": "T"})
    assert state
    args = ["record", "--kind", "command", "--command", "uptime", "--text", "up 3 days"]
    assert audit.main([*args, "--agent", "codex", "--model", "openai/gpt-5"]) == 0
    record = log("catalog-5")[-1]
    assert (record["agent"], record["model"], record["capture"]) == ("codex", "openai/gpt-5", "self-reported")
