"""The strict matcher that judges an agent's tool calls against the chat policy's rule lists."""

from __future__ import annotations

from pathlib import Path

import chat_policy
import pytest
import tool_policy
from tool_policy import ALLOW, ASK, DENY

SCRIPTS = Path("/opt/plugin/scripts")
SITE_RULES = {"allow": ["mcp__nagios__get_alerts", "Bash(ssh macro uptime)"]}


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("secret", encoding="utf-8")
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "site.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "site" / "innocent").symlink_to(tmp_path / ".ssh" / "id_ed25519")
    return tmp_path


@pytest.fixture
def chat(home):
    return tool_policy.Policy.from_rules(chat_policy.rules(SITE_RULES, SCRIPTS), home=home)


@pytest.fixture
def triage(home):
    return tool_policy.Policy.from_rules(chat_policy.triage_rules(SITE_RULES, SCRIPTS), home=home)


def bash(policy, command, cwd):
    return tool_policy.decide(policy, "bash", {"command": command}, cwd).decision


HARNESS = f"uv run {SCRIPTS}"


@pytest.mark.parametrize(
    "command",
    [
        f'{HARNESS}/timeline.py add --type discovery --text "pods restarted; ingress fine"',
        f"{HARNESS}/kube_read.py get pods -n web",
        "gh issue view 7 --repo o/r",
        "date",
        "uptime",
        "ssh macro uptime",
    ],
)
def test_one_plain_allowed_command_runs(chat, home, command):
    assert bash(chat, command, home / "site") == ALLOW


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("cat /etc/hostname", "Claude Code's built-in read-only set is not allowed"),
        ("gh issue view 7 && rm -rf /", "chaining"),
        ("gh issue view 7 | sh", "pipe"),
        ("gh issue view 7 > /tmp/out", "redirect"),
        ("gh issue view 7 &", "background"),
        ("gh issue view $(id -u)", "substitution"),
        ("gh issue view `id -u`", "backtick"),
        ("gh issue view $HOME", "variable"),
        ("gh issue view 7\nrm -rf /", "newline"),
        ("ssh macro uptime; reboot", "an exact rule never takes arguments or chains"),
        ("ssh macro reboot", "an exact rule is not a prefix"),
        ("gh  issue view 7", "spacing changes the prefix"),
        ("", "empty"),
    ],
)
def test_anything_less_than_one_plain_allowed_command_asks(chat, home, command, why):
    assert bash(chat, command, home / "site") == ASK, why


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv AWS_SECRET_ACCESS_KEY",
        "gh auth token",
        "kubectl get secrets -A",
        "kubectl get pods,secrets",
        "kubectl get pods --token=abc",
        "kubectl get pods -s https://elsewhere",
        "kubectl exec -it web -- sh",
        "uptime && env",
        "date; printenv",
    ],
)
def test_denied_commands_are_refused_wherever_they_appear(chat, home, command):
    assert bash(chat, command, home / "site") == DENY


def test_triage_opens_only_privately_and_never_closes_or_acts(triage, home):
    site = home / "site"
    assert bash(triage, f"{HARNESS}/incident.py open --auto --title T --severity P3", site) == ALLOW
    assert bash(triage, f"{HARNESS}/incident.py open --title T --severity P3", site) == ASK
    assert bash(triage, f"{HARNESS}/incident.py close", site) == DENY
    assert bash(triage, f"{HARNESS}/incident.py promote --by x", site) == DENY
    assert bash(triage, f"{HARNESS}/autonomy.py run --command x", site) == DENY
    assert bash(triage, f"{HARNESS}/timeline.py add --type discovery --text x", site) == ALLOW


def path_call(policy, tool, path, cwd):
    return tool_policy.decide(policy, tool, {"path": str(path)}, cwd).decision


def test_reads_inside_the_site_run_and_secrets_are_refused(chat, home):
    site = home / "site"
    assert path_call(chat, "read", site / "site.yaml", site) == ALLOW
    assert path_call(chat, "read", home / ".ssh" / "id_ed25519", site) == DENY
    assert path_call(chat, "read", "site.yaml", site) == ALLOW
    assert path_call(chat, "read", home / "proj" / ".env", site) == DENY
    assert path_call(chat, "read", "/proc/self/environ", site) == DENY


def test_a_symlink_is_judged_by_where_it_leads(chat, home):
    assert path_call(chat, "read", home / "site" / "innocent", home / "site") == DENY


def test_a_read_rule_also_protects_a_path_from_editing(chat, home):
    assert path_call(chat, "edit", home / ".ssh" / "authorized_keys", home / "site") == DENY
    assert path_call(chat, "write", home / "site" / "notes.md", home / "site") == ASK


def test_a_search_from_above_a_denied_directory_asks(chat, home):
    assert path_call(chat, "grep", home, home / "site") == ASK
    assert path_call(chat, "find", home, home / "site") == ASK
    assert path_call(chat, "grep", home / "site", home / "site") == ALLOW
    assert tool_policy.decide(chat, "grep", {"pattern": "x"}, home / "site").decision == ALLOW


def test_a_glob_pattern_that_escapes_is_judged_where_it_leads(chat, home):
    site = home / "site"
    escape = tool_policy.decide(chat, "find", {"pattern": f"{home}/.ssh/*"}, site).decision
    assert escape == DENY
    assert tool_policy.decide(chat, "find", {"pattern": "../.ssh/*", "path": str(site)}, site).decision == DENY
    assert tool_policy.decide(chat, "find", {"pattern": f"{home}/*"}, site).decision == ASK
    assert tool_policy.decide(chat, "find", {"pattern": "**/*.yaml"}, site).decision == ALLOW


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('gh issue view "7 && rm -rf /"', ALLOW),
        ("gh issue view 7 <<< x", ASK),
        ("GH_TOKEN=x gh issue view 7", ASK),
        ("gh issue view 7", ASK),
    ],
)
def test_more_shell_shapes(chat, home, command, expected):
    assert bash(chat, command, home / "site") == expected


def test_mcp_and_other_tools_need_a_rule_naming_them(chat, home):
    site = home / "site"
    assert tool_policy.decide(chat, "mcp__nagios__get_alerts", {}, site).decision == ALLOW
    assert tool_policy.decide(chat, "mcp__nagios__acknowledge_problem", {}, site).decision == ASK
    assert tool_policy.decide(chat, "WebFetch", {"url": "https://x"}, site).decision == DENY
    assert tool_policy.decide(chat, "codemode", {"code": "x"}, site).decision == ASK


def test_rules_that_are_not_rules_are_rejected():
    with pytest.raises(ValueError):
        tool_policy.parse("Bash(unclosed")
