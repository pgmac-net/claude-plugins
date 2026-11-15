"""Tests for approvals recorded by a chat gateway, and how verify credits them."""

from __future__ import annotations

import hashlib
import json

import audit
import audit_hook
import pytest
from sre_common import SiteError

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
APPROVER = "U" + "0ABCDEF12"
INCIDENT = "incidents-7"


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    audit_hook.bind("s1", INCIDENT)
    return tmp_path / "state" / "sre-harness"


def hook(event: str, command: str, tool_use_id: str) -> None:
    payload = {
        "session_id": "s1",
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": tool_use_id,
    }
    if event == "PostToolUse":
        payload["tool_response"] = {"stdout": "", "stderr": ""}
    audit_hook.handle(payload)


def decide(command: str, tool_use_id: str, outcome: str = "approved", **extra) -> dict:
    return audit.record_chat_decision(
        INCIDENT,
        audit.ChatDecision(
            outcome=outcome,
            tool="Bash",
            tool_input={"command": command},
            tool_use_id=tool_use_id,
            approver=extra.pop("approver", APPROVER),
            link="https://chat.example.com/archives/C1/p1",
            **extra,
        ),
    )


def entries() -> list[tuple[str, dict]]:
    return audit.read_log(INCIDENT)


def test_hook_records_the_tool_use_id():
    hook("PreToolUse", "uptime", "toolu_1")
    assert entries()[-1][1]["tool_use_id"] == "toolu_1"


def test_approval_after_its_attempt_credits_that_call():
    """The hook records the attempt before the permission check, so the approval comes second."""
    hook("PreToolUse", "kubectl rollout restart deploy/web", "toolu_1")
    decide("kubectl rollout restart deploy/web", "toolu_1")
    hook("PostToolUse", "kubectl rollout restart deploy/web", "toolu_1")
    assert audit.unauthorised_mutations(entries()) == []


def test_approval_covers_only_its_own_call():
    decide("kubectl rollout restart deploy/web", "toolu_1")
    hook("PreToolUse", "kubectl rollout restart deploy/web", "toolu_1")
    hook("PreToolUse", "kubectl rollout restart deploy/web", "toolu_2")
    found = audit.unauthorised_mutations(entries())
    assert len(found) == 1


def test_approval_does_not_cover_a_different_input_with_the_same_id():
    decide("kubectl rollout restart deploy/web", "toolu_1")
    hook("PreToolUse", "kubectl delete deploy/web", "toolu_1")
    assert len(audit.unauthorised_mutations(entries())) == 1


@pytest.mark.parametrize("outcome", ["denied", "expired"])
def test_denial_and_expiry_credit_nothing(outcome):
    approver = None if outcome == "expired" else APPROVER
    decide("kubectl rollout restart deploy/web", "toolu_1", outcome=outcome, approver=approver)
    hook("PreToolUse", "kubectl rollout restart deploy/web", "toolu_1")
    assert len(audit.unauthorised_mutations(entries())) == 1
    record = entries()[0][1]
    assert record["event"] == "denial"
    assert record["outcome"] == outcome


def test_record_names_approver_link_and_exact_input_without_the_secret():
    command = f"curl -H 'Authorization: Bearer {TOKEN}' https://api.example.com/x"
    record = decide(command, "toolu_9")
    line = entries()[-1][0]
    assert TOKEN not in line
    assert record["approver"] == APPROVER
    assert record["link"].startswith("https://")
    assert record["capture"] == "gateway"
    exact = json.dumps({"command": command}, sort_keys=True, ensure_ascii=False)
    assert record["input_sha256"] == hashlib.sha256(exact.encode()).hexdigest()


def test_record_redacts_like_the_hook_so_credit_matches(monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "s3cr3t-value-123")
    command = "kubectl rollout restart deploy/web --token s3cr3t-value-123"
    hook("PreToolUse", command, "toolu_1")
    decide(command, "toolu_1")
    assert audit.unauthorised_mutations(entries()) == []
    assert "s3cr3t-value-123" not in entries()[1][0]


def test_reason_is_kept_and_redacted():
    record = decide("rm -rf /tmp/x", "toolu_1", outcome="denied", reason=f"no, and token={TOKEN}")
    assert record["reason"].startswith("no")
    assert TOKEN not in record["reason"]


def test_decision_needs_an_approver_unless_expired():
    with pytest.raises(ValueError, match="identity"):
        decide("rm /tmp/x", "toolu_1", approver=None)


def test_unknown_outcome_is_refused():
    with pytest.raises(ValueError):
        decide("rm /tmp/x", "toolu_1", outcome="maybe")


def test_closed_log_takes_no_decision(state):
    (state / "audit").mkdir(parents=True, exist_ok=True)
    (state / "audit" / f"{INCIDENT}.closed").write_text("", encoding="utf-8")
    with pytest.raises(SiteError, match="closed"):
        decide("rm /tmp/x", "toolu_1")


def test_self_reported_record_with_a_chat_shape_does_not_use_the_id_path():
    """Only a gateway record ('via: chat') credits by call id; a plain approval still credits by command."""
    audit_hook.self_report(INCIDENT, "approval", command="kubectl delete pod web-1", tool_use_id="toolu_1")
    hook("PreToolUse", "kubectl delete pod web-2", "toolu_1")
    assert len(audit.unauthorised_mutations(entries())) == 1


def test_chain_stays_intact_with_gateway_records():
    hook("PreToolUse", "kubectl rollout restart deploy/web", "toolu_1")
    decide("kubectl rollout restart deploy/web", "toolu_1")
    result = audit.verify(entries())
    assert result["chain_intact"]
    assert result["approvals"] == 1
