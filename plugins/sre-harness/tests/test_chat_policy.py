"""Tests for chat_policy.py and the chat section of site validation."""

from __future__ import annotations

import json
from pathlib import Path

import chat_policy
import pytest
import validate_site as vs

USER = "U" + "0ABCDEF12"
OTHER = "U" + "0ZYXWVU98"
CHANNEL = "C" + "0HUB12345"


def chat(**overrides) -> dict:
    base = {
        "adapter": "slack",
        "hub_channel": CHANNEL,
        "responders": [USER, OTHER],
        "approvers": [USER],
        "secrets_env": {"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"},
    }
    base.update(overrides)
    return base


# --- the rules --------------------------------------------------------------


def test_default_allow_names_harness_scripts_by_absolute_path():
    allow = chat_policy.default_allow(Path("/opt/plugin/scripts"))
    assert "Bash(uv run /opt/plugin/scripts/incident.py open *)" in allow
    assert "Bash(uv run /opt/plugin/scripts/timeline.py *)" in allow
    assert "Bash(uv run /opt/plugin/scripts/kube_read.py *)" in allow


def test_plain_kubectl_is_not_allow_listed():
    """Its options can redirect the credential (-s bundled with other short flags); kube_read.py checks them."""
    assert not any(rule.startswith("Bash(kubectl") for rule in chat_policy.default_allow(Path("/p")))


def test_the_agent_cannot_record_its_own_approval():
    allow = chat_policy.default_allow(Path("/p"))
    assert not any("audit.py record" in rule or "audit.py commit" in rule for rule in allow)


def test_no_mutation_and_no_remote_shell_in_the_default_allow_list():
    for rule in chat_policy.default_allow(Path("/p")):
        if rule.startswith("Bash(") and "/p/" not in rule:
            prefix = rule[5:-1].removesuffix(" *")
            assert not chat_policy.audit.is_mutation(prefix), rule
            assert not prefix.startswith(("ssh", "sudo")), rule


def test_no_default_rule_for_commands_with_file_reading_or_writing_options():
    """git diff --no-index, git log --output and journalctl --file/--vacuum reach outside the deny rules."""
    allow = chat_policy.default_allow(Path("/p"))
    assert not any(rule.startswith(("Bash(git ", "Bash(journalctl")) for rule in allow)


def test_kubectl_cannot_be_pointed_at_another_credential():
    deny = chat_policy.default_deny()
    for option in ("--kubeconfig", "--token", "--server", "--as", "--user", "--cluster", "--insecure-skip-tls-verify"):
        assert f"Bash(kubectl *{option}*)" in deny
    assert "Bash(kubectl * -s*)" in deny


def test_deny_list_covers_credentials_secrets_and_the_web():
    deny = chat_policy.default_deny()
    for rule in (
        "Read(~/.ssh/**)",
        "Read(~/.config/sre-harness/**)",
        "Read(~/.local/state/sre-harness/**)",
        "Bash(kubectl get *secret*)",
        "Bash(kubectl exec *)",
        "Bash(gh auth *)",
        "Bash(printenv *)",
        "WebFetch",
        "WebSearch",
    ):
        assert rule in deny


def test_settings_json_is_permissions_only_and_merges_site_rules():
    settings = json.loads(chat_policy.settings_json(chat(allow=["mcp__monitoring"]), Path("/p")))
    assert set(settings) == {"permissions"}
    assert "mcp__monitoring" in settings["permissions"]["allow"]
    assert "WebFetch" in settings["permissions"]["deny"]


def test_rules_refuse_a_bad_site_rule():
    with pytest.raises(ValueError, match="chat/allow/0"):
        chat_policy.rules(chat(allow=["Bash"]))


# --- what a site may add ------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "mcp__monitoring",
        "mcp__monitoring__get_alerts",
        "Read(//srv/runbooks/**)",
        "Grep(//srv/runbooks/**)",
        "Bash(zpool status *)",
        "Bash(docker ps *)",
    ],
)
def test_acceptable_site_rules(rule):
    assert chat_policy.check_site_rule(rule) == (None, None)


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("Bash", "must be"),
        ("Bash(*)", "must be"),
        ("Bash(* --help)", "must be"),
        ("Edit(//etc/**)", "must be"),
        ("Write", "must be"),
        ("WebFetch", "must be"),
        ("Bash(zpool *)", "two words"),
        ("Bash(kubectl get *)", "kube_read.py"),
        ("Bash(kubectl get --raw /healthz)", "kube_read.py"),
        ("Bash(helm list *)", "another server"),
        ("Bash(git log *)", "any file"),
        ("Bash(journalctl -u nginx *)", "any file"),
        ("Bash(sudo systemctl status *)", "runs other commands"),
        ("Bash(bash -c *)", "runs other commands"),
        ("Bash(python3 -m http *)", "runs other commands"),
        ("Bash(docker rm web *)", "mutation"),
        ("Bash(systemctl restart nginx)", "mutation"),
        ("Bash(gh auth status *)", "deny-list"),
        ("Bash(zpool status; rm -rf / *)", "shell operators"),
        ("Bash(echo $HOME *)", "shell operators"),
    ],
)
def test_refused_site_rules(rule, why):
    problem, _warning = chat_policy.check_site_rule(rule)
    assert problem is not None
    assert why in problem


def test_a_remote_shell_rule_is_allowed_with_a_warning():
    problem, warning = chat_policy.check_site_rule("Bash(ssh backup-host *)")
    assert problem is None
    assert "remote shell" in warning


# --- site validation ----------------------------------------------------------


def errors_and_warnings(edit_site, section: dict) -> tuple[list[str], list[str]]:
    report = vs.validate(edit_site(lambda doc: doc.update({"chat": section})))
    return report.errors, report.warnings


def test_empty_chat_means_no_gateway(site):
    report = vs.validate(site)
    assert report.summary["chat_configured"] is False
    assert report.summary["chat"] is None


def test_a_full_chat_section_is_valid(edit_site):
    report = vs.validate(edit_site(lambda doc: doc.update({"chat": chat(allow=["mcp__monitoring"])})))
    assert report.errors == []
    assert report.summary["chat"] == {"adapter": "slack", "responders": 2, "approvers": 1, "site_allow_rules": 1}


def test_a_partial_chat_section_is_refused(edit_site):
    errors, _ = errors_and_warnings(edit_site, {"adapter": "slack"})
    assert any("hub_channel" in err for err in errors)


def test_an_approver_must_be_a_responder(edit_site):
    stranger = "U" + "0STRANGE9"
    errors, _ = errors_and_warnings(edit_site, chat(approvers=[USER, stranger]))
    assert any("chat/approvers/1: not listed in chat/responders" in err for err in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("hub_channel", "incidents", "chat/hub_channel: not a slack channel ID"),
        ("responders", [USER, "paul"], "chat/responders/1: not a slack user ID"),
    ],
)
def test_ids_must_have_the_platform_shape(edit_site, field, value, message):
    errors, _ = errors_and_warnings(edit_site, chat(**{field: value}))
    assert any(message in err for err in errors)


def test_tokens_need_their_own_variables(edit_site):
    errors, _ = errors_and_warnings(edit_site, chat(secrets_env={"bot_token": "SLACK", "app_token": "SLACK"}))
    assert any("own environment variable" in err for err in errors)


def test_a_token_in_the_chat_section_is_refused_without_echo(edit_site):
    token = "xoxb-" + "123456789012-abcdefghijkl"
    errors, _ = errors_and_warnings(edit_site, chat(secrets_env={"bot_token": token, "app_token": "X"}))
    assert errors
    assert all(token not in err for err in errors)


def test_bad_and_risky_site_rules_surface_in_validation(edit_site):
    errors, warnings = errors_and_warnings(edit_site, chat(allow=["Bash(kubectl get *)", "Bash(ssh backup-host *)"]))
    assert any("chat/allow/0" in err for err in errors)
    assert any("chat/allow/1" in warning for warning in warnings)


@pytest.mark.parametrize("ttl", [10, 99999])
def test_approval_ttl_is_bounded(edit_site, ttl):
    errors, _ = errors_and_warnings(edit_site, chat(approval_ttl_seconds=ttl))
    assert any("approval_ttl_seconds" in err for err in errors)
