"""Configuration, the link to the harness, and the SDK driver's mapping of messages to events."""

# The driver's mapping and permission callback are tested directly.
# pylint: disable=protected-access

from __future__ import annotations

import json
import os
import shutil

import pytest
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from sre_gateway import config as config_module
from sre_gateway import harness
from sre_gateway.__main__ import agent_options
from sre_gateway.agent import Notice, PermissionAnswer, SdkDriver, Text, ToolCall, ToolOutput, TurnEnd

USER = "U" + "0OWNER001"
CHANNEL = "C" + "0HUB00001"
BOT = "xoxb-" + "123456789012-abcdefghijkl"
APP = "xapp-" + "1-A0123456789-0123456789"


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "site"
    shutil.copytree(harness.PLUGIN_ROOT / "templates" / "site-repo", root)
    doc = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))
    doc["chat"] = {
        "adapter": "slack",
        "hub_channel": CHANNEL,
        "responders": [USER],
        "approvers": [USER],
        "approval_ttl_seconds": 120,
        "secrets_env": {"bot_token": "TEST_SLACK_BOT", "app_token": "TEST_SLACK_APP"},
    }
    (root / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return root


def env_for(site, **extra):
    return {"SRE_HARNESS_SITE": str(site), "TEST_SLACK_BOT": BOT, "TEST_SLACK_APP": APP, **extra}


# --- configuration ----------------------------------------------------------------


def test_load_reads_chat_and_takes_tokens_out_of_the_environment(site, monkeypatch):
    monkeypatch.setenv("TEST_SLACK_BOT", BOT)
    monkeypatch.setenv("TEST_SLACK_APP", APP)
    config = config_module.load(env_for(site))
    assert config.tokens == {"bot_token": BOT, "app_token": APP}
    assert config.approval_ttl == 120
    assert config.idle_stop_seconds == 30 * 60
    assert config.max_sessions == 2
    assert config.approvers == frozenset({USER})
    assert "TEST_SLACK_BOT" not in os.environ
    assert "TEST_SLACK_APP" not in os.environ
    assert BOT not in repr(config)


def test_missing_token_is_an_error(site):
    env = env_for(site)
    del env["TEST_SLACK_APP"]
    with pytest.raises(config_module.ConfigError, match="TEST_SLACK_APP"):
        config_module.load(env, take_tokens=False)


def test_no_chat_section_is_an_error(site):
    doc = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    doc["chat"] = {}
    (site / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(config_module.ConfigError, match="no chat section"):
        config_module.load(env_for(site), take_tokens=False)


def test_invalid_site_is_an_error(site):
    doc = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    doc["chat"]["approvers"] = ["U" + "0STRANGE1"]
    (site / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(config_module.ConfigError, match="not valid: .*chat/approvers/0: not listed"):
        config_module.load(env_for(site), take_tokens=False)


def test_agent_options_load_only_the_chat_policy(site):
    config = config_module.load(env_for(site), take_tokens=False)
    options = agent_options(config, "session-9", None)
    assert options.setting_sources == []
    assert options.permission_mode == "default"
    assert options.resume == "session-9"
    assert options.plugins == [{"type": "local", "path": str(harness.PLUGIN_ROOT)}]
    assert options.cwd == str(config.site_root)
    permissions = json.loads(options.settings)["permissions"]
    assert "WebFetch" in permissions["deny"]
    assert any("kube_read.py" in rule for rule in permissions["allow"])
    assert "kube_read.py" in options.system_prompt["append"]
    assert options.env == {"SRE_HARNESS_SITE": str(config.site_root), "SRE_HARNESS_NO_CURRENT": "1"}
    assert "--incident <id>" in options.system_prompt["append"]
    assert BOT not in json.dumps(options.env)


def test_triage_options_narrow_the_rules_and_flag_the_scripts(site):
    config = config_module.load(env_for(site), take_tokens=False)
    options = agent_options(config, None, None, triage="ab" * 16)
    assert options.env[harness.sre_common.TRIAGE_ENV] == "ab" * 16
    permissions = json.loads(options.settings)["permissions"]
    assert any(rule.endswith("incident.py open --auto *)") for rule in permissions["allow"])
    assert any(rule.endswith("incident.py close *)") for rule in permissions["deny"])
    assert "UNATTENDED TRIAGE" in options.system_prompt["append"]


def test_question_options_use_the_question_rules_and_instructions(site):
    config = config_module.load(env_for(site), take_tokens=False)
    options = agent_options(config, None, None, question=True)
    assert harness.sre_common.TRIAGE_ENV not in options.env
    permissions = json.loads(options.settings)["permissions"]
    assert not any("incident.py open" in rule or "timeline.py" in rule for rule in permissions["allow"])
    assert any(rule.endswith("timeline.py *)") for rule in permissions["deny"])
    assert any("slo.py status" in rule for rule in permissions["allow"])
    assert "This is a QUESTION, not an incident" in options.system_prompt["append"]
    assert "start-incident skill. Open the record" not in options.system_prompt["append"]
    assert config.question_idle_seconds == 300


# --- the harness link -------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("uv run /opt/p/scripts/incident.py open --title x --severity P2", "open"),
        ("uv run /opt/p/scripts/incident.py attach incidents-7", "attach"),
        ("uv run /opt/p/scripts/incident.py close", "close"),
        ("uv run /opt/p/scripts/incident.py status", None),
        ("echo incident.py open", None),
    ],
)
def test_incident_calls_are_recognised(command, kind):
    assert harness.incident_call(command) == kind


@pytest.mark.usefixtures("site")
def test_opened_incident_needs_matching_local_state():
    state_file = harness.sre_common.incident_state_path("incidents-7")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"id": "incidents-7", "url": "https://example.com/7"}), encoding="utf-8")
    good = json.dumps({"id": "incidents-7", "url": "https://example.com/7", "bind": "0" * 32})
    assert harness.opened_incident(f"Cleared state.\n{good}\n")["id"] == "incidents-7"
    forged_url = json.dumps({"id": "incidents-7", "url": "https://elsewhere/7"})
    assert harness.opened_incident(forged_url) is None
    assert harness.opened_incident(json.dumps({"id": "../../etc", "url": "x"})) is None
    assert harness.opened_incident("not json") is None


def test_scrub_removes_tokens_without_a_digest():
    secrets = harness.outbound_secrets(extra={"bot_token": BOT})
    text = harness.scrub(f"token {BOT} here", secrets)
    assert BOT not in text
    assert "sha256" not in text


# --- the SDK driver ----------------------------------------------------------------


def test_sdk_messages_become_events():
    driver = SdkDriver(lambda resume, can_use: None, None)
    assert driver._events(SystemMessage("init", {"session_id": "s-1"})) == []
    assert driver.session_id == "s-1"
    assistant = AssistantMessage(
        [TextBlock("Looking."), TextBlock("  "), ToolUseBlock("t-1", "Bash", {"command": "date"})], "m"
    )
    assert driver._events(assistant) == [Text("Looking."), ToolCall("t-1", "Bash", {"command": "date"})]
    user = UserMessage([ToolResultBlock("t-1", [{"type": "text", "text": "Mon"}], False)])
    assert driver._events(user) == [ToolOutput("t-1", "Mon", False)]
    limited = AssistantMessage([], "m", error="rate_limit")
    assert isinstance(driver._events(limited)[0], Notice)
    result = ResultMessage("error_max_turns", 1, 1, True, 3, "s-2", total_cost_usd=0.01)
    assert driver._events(result) == [TurnEnd("s-2", "error_max_turns", 0.01)]
    auth = ResultMessage("success", 1, 1, True, 1, "s-2", api_error_status=401)
    assert driver._events(auth)[0].error == "API error 401"
    failed = ResultMessage("success", 1, 1, True, 1, "s-2")
    assert driver._events(failed)[0].error == "the turn ended with an error"
    fine = ResultMessage("success", 1, 1, False, 1, "s-2")
    assert driver._events(fine)[0].error is None
    assert driver.session_id == "s-2"


async def test_permission_callback_binds_to_the_call_and_never_changes_input():
    seen = []

    async def ask(tool, data, call_id):
        seen.append((tool, data, call_id))
        return PermissionAnswer(data["command"] == "touch ok", "no")

    driver = SdkDriver(lambda resume, can_use: None, ask)
    allowed = await driver._can_use_tool("Bash", {"command": "touch ok"}, ToolPermissionContext(tool_use_id="t-1"))
    assert isinstance(allowed, PermissionResultAllow)
    assert allowed.updated_input == {"command": "touch ok"}
    denied = await driver._can_use_tool("Bash", {"command": "rm x"}, ToolPermissionContext(tool_use_id="t-2"))
    assert isinstance(denied, PermissionResultDeny)
    assert denied.message == "no"
    unbound = await driver._can_use_tool("Bash", {"command": "touch ok"}, ToolPermissionContext())
    assert isinstance(unbound, PermissionResultDeny)
    assert len(seen) == 2
