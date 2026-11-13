"""Tests for autonomy.py and the unauthorised-mutation report."""

from __future__ import annotations

import json

import audit
import audit_hook
import autonomy
import pytest
import sre_common
import validate_site as vs
import yaml

ACTION = {
    "id": "restart-shop-pod",
    "service": "component:example-service",
    "runbook": "shop-stuck.md",
    "command": "kubectl --context {cluster} delete pod {pod} -n {namespace}",
    "parameters": {
        "cluster": {"values": ["main"]},
        "namespace": {"values": ["shop"]},
        "pod": {"pattern": "shop-[a-z0-9]+-[a-z0-9]+"},
    },
    "precondition": "The pod is in CrashLoopBackOff and its volume is healthy.",
    "max_per_incident": 2,
}
GOOD = "kubectl --context main delete pod shop-7f9c-x2k4 -n shop"


def allow(**overrides) -> dict:
    base = {"schema_version": 1, "tier2_enabled": True, "actions": [dict(ACTION)]}
    base.update(overrides)
    return base


def decide(
    command: str, allow_list: dict | None = None, service: str | None = None, incident: str | None = None
) -> dict:
    return autonomy.decide(allow_list or allow(), command, service, incident)


def test_exact_match_is_tier_two():
    result = decide(GOOD)
    assert result["tier"] == 2
    assert result["action"] == "restart-shop-pod"
    assert result["precondition"].startswith("The pod is in CrashLoopBackOff")


@pytest.mark.parametrize(
    "command",
    [
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop --force",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop --grace-period=0",
        "kubectl --context main delete pod shop-7f9c-x2k4",
        "kubectl --context other delete pod shop-7f9c-x2k4 -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n kube-system",
        "kubectl --context main delete pod db-7f9c-x2k4 -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4,db-0 -n shop",
        "kubectl --context main delete pods shop-7f9c-x2k4 -n shop",
        "kubectl --context main delete deployment shop-7f9c-x2k4 -n shop",
        "sudo kubectl --context main delete pod shop-7f9c-x2k4 -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop; rm -rf /",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop && reboot",
        "kubectl --context main delete pod $(cat /tmp/x) -n shop",
        "kubectl --context main delete pod `whoami` -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop > /etc/passwd",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop\nreboot",
        "kubectl --context main delete pod 'shop-7f9c-x2k4 -n kube-system' -n shop",
        "kubectl --context main delete pod $POD -n shop",
        "",
        "kubectl --context main delete pod 'unterminated",
    ],
)
def test_anything_but_the_exact_command_is_tier_one(command):
    result = decide(command)
    assert result["tier"] == 1
    assert result["reason"]


def test_quoting_that_yields_the_same_tokens_still_matches():
    assert decide("kubectl --context 'main' delete pod \"shop-7f9c-x2k4\" -n shop")["tier"] == 2


def test_switches_turn_tier_two_off():
    assert decide(GOOD, allow(tier2_enabled=False))["reason"] == "Tier 2 is switched off for this site"
    suspended = decide(GOOD, allow(disabled_services=["component:example-service"]))
    assert suspended["tier"] == 1 and "suspended" in suspended["reason"]
    disabled = decide(GOOD, allow(actions=[{**ACTION, "enabled": False}]))
    assert disabled["tier"] == 1 and "disabled" in disabled["reason"]


def test_service_must_match_when_given():
    assert decide(GOOD, service="component:example-service")["tier"] == 2
    other = decide(GOOD, service="component:billing")
    assert other["tier"] == 1 and "not component:billing" in other["reason"]


def test_placeholder_without_a_rule_never_matches():
    loose = {**ACTION, "parameters": {"cluster": {"values": ["main"]}, "namespace": {"values": ["shop"]}}}
    result = decide(GOOD, allow(actions=[loose]))
    assert result["tier"] == 1 and "no rule for {pod}" in result["reason"]


def test_repeated_placeholder_must_take_one_value():
    twice = {
        **ACTION,
        "command": "tool --from {name} --to {name}",
        "parameters": {"name": {"pattern": "[a-z]+"}},
    }
    assert decide("tool --from abc --to abc", allow(actions=[twice]))["tier"] == 2
    assert decide("tool --from abc --to xyz", allow(actions=[twice]))["tier"] == 1


def test_embedded_placeholder_matches_within_a_token():
    embedded = {**ACTION, "command": "tool --ns={namespace} restart", "parameters": {"namespace": {"values": ["shop"]}}}
    assert decide("tool --ns=shop restart", allow(actions=[embedded]))["tier"] == 2
    assert decide("tool --ns=shop,other restart", allow(actions=[embedded]))["tier"] == 1


@pytest.fixture
def incident(monkeypatch, tmp_path, site):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
    (site / "autonomy.yaml").write_text(yaml.safe_dump(allow()), encoding="utf-8")
    sre_common.write_incident_state({"id": "catalog-5", "repo": "o/r", "number": 5, "url": "u", "title": "T"})
    return site


@pytest.mark.usefixtures("incident")
class TestClaim:
    def claim(self, capsys, *extra: str) -> dict:
        assert autonomy.main(["claim", "--command", GOOD, *extra]) == 0
        return json.loads(capsys.readouterr().out)

    def test_claim_needs_evidence_of_the_precondition(self, capsys):
        result = self.claim(capsys)
        assert result["tier"] == 1 and "no --precondition-evidence" in result["reason"]
        assert not audit_hook.spool_path("catalog-5").exists()

    def test_claim_records_and_enforces_the_limit(self, capsys):
        evidence = ["--precondition-evidence", "kubectl get pod showed CrashLoopBackOff; volume Ready"]
        assert self.claim(capsys, *evidence)["used"] == 1
        assert self.claim(capsys, *evidence)["used"] == 2
        third = self.claim(capsys, *evidence)
        assert third["tier"] == 1 and "reached its limit of 2" in third["reason"]
        records = [json.loads(line) for line in audit_hook.spool_path("catalog-5").read_text().splitlines()]
        assert [(r["event"], r["action"], r["command"]) for r in records] == [("tier2", "restart-shop-pod", GOOD)] * 2

    def test_match_changes_nothing(self, capsys):
        assert autonomy.main(["match", "--command", GOOD]) == 0
        assert json.loads(capsys.readouterr().out)["tier"] == 2
        assert not audit_hook.spool_path("catalog-5").exists()

    def test_site_without_an_allow_list_is_all_tier_one(self, incident, capsys):
        doc = yaml.safe_load((incident / "site.yaml").read_text())
        del doc["incident"]["autonomy_file"]
        (incident / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        assert autonomy.main(["match", "--command", GOOD]) == 0
        assert json.loads(capsys.readouterr().out)["tier"] == 1


class TestMutationDetection:
    @pytest.mark.parametrize(
        "command",
        [
            GOOD,
            "kubectl rollout restart deployment/shop -n shop",
            "ssh node1 'sudo systemctl restart kubelet'",
            "helm upgrade shop ./chart",
            "docker compose up -d",
            "terraform apply -auto-approve",
            "ansible-playbook -i hosts site.yml",
            "sudo reboot",
            "rm -rf /var/lib/thing",
            "echo x | sudo tee /etc/thing.conf",
            "git push origin main",
        ],
    )
    def test_mutations_are_recognised(self, command):
        assert audit.is_mutation(command)

    @pytest.mark.parametrize(
        "command",
        [
            "kubectl --context main get pods -A",
            "kubectl describe pod shop-1 -n shop",
            "kubectl logs shop-1 -n shop --tail=50",
            "journalctl -u kubelet --since '10 min ago'",
            "systemctl status kubelet",
            "docker ps",
            "git log --oneline -5",
            "terraform plan",
            "uv run /p/scripts/timeline.py add --type action --text 'deleted pod; rm of stale file'",
            "uv run /p/scripts/incident.py set --phase Mitigating",
        ],
    )
    def test_diagnostics_and_record_keeping_are_not(self, command):
        assert not audit.is_mutation(command)

    def entries(self, *records: dict) -> list[tuple[str, dict]]:
        return [(json.dumps(r), {"ts": "2026-01-02T03:04:05Z", **r}) for r in records]

    def attempt(self, command: str) -> dict:
        return {"event": "attempt", "tool": "Bash", "capture": "hook", "input": {"command": command}}

    def test_mutation_with_no_authority_is_reported(self):
        found = audit.unauthorised_mutations(self.entries(self.attempt("kubectl get pods"), self.attempt(GOOD)))
        assert [item["command"] for item in found] == [GOOD]

    def test_approval_or_claim_authorises_exactly_one_run_and_must_come_first(self):
        approval = {"event": "approval", "command": GOOD, "capture": "self-reported"}
        claim = {"event": "tier2", "command": GOOD, "action": "restart-shop-pod"}
        assert audit.unauthorised_mutations(self.entries(approval, self.attempt(GOOD))) == []
        assert audit.unauthorised_mutations(self.entries(claim, self.attempt(GOOD))) == []
        assert len(audit.unauthorised_mutations(self.entries(approval, self.attempt(GOOD), self.attempt(GOOD)))) == 1
        assert len(audit.unauthorised_mutations(self.entries(self.attempt(GOOD), approval))) == 1

    def test_approval_for_a_different_command_does_not_count(self):
        approval = {"event": "approval", "command": GOOD + " --force"}
        assert len(audit.unauthorised_mutations(self.entries(approval, self.attempt(GOOD)))) == 1

    def test_self_reported_commands_are_checked_too(self):
        reported = {"event": "command", "command": "sudo reboot", "capture": "self-reported"}
        assert len(audit.unauthorised_mutations(self.entries(reported))) == 1


class TestAllowListValidation:
    def errors(self, site, allow_list: dict) -> list[str]:
        (site / "autonomy.yaml").write_text(yaml.safe_dump(allow_list), encoding="utf-8")
        return vs.validate(site).errors

    def test_valid_allow_list(self, site):
        report_errors = self.errors(site, allow())
        assert report_errors == []
        assert vs.validate(site).summary["incident"]["autonomy"] == {
            "tier2_enabled": True,
            "actions": 1,
            "enabled_actions": 1,
        }

    def test_unknown_service_is_rejected(self, site):
        bad = allow(actions=[{**ACTION, "service": "component:nope"}])
        assert any("service component:nope is not in the catalog" in err for err in self.errors(site, bad))

    def test_shell_operators_in_a_template_are_rejected(self, site):
        bad = allow(actions=[{**ACTION, "command": ACTION["command"] + " && reboot"}])
        assert any("shell operators" in err for err in self.errors(site, bad))

    def test_placeholders_and_parameters_must_agree(self, site):
        params = {"cluster": {"values": ["main"]}, "namespace": {"values": ["shop"]}, "extra": {"values": ["x"]}}
        found = self.errors(site, allow(actions=[{**ACTION, "parameters": params}]))
        assert any("placeholder {pod} has no rule" in err for err in found)
        assert any("parameter 'extra' is not used" in err for err in found)

    def test_duplicate_ids_and_bad_patterns_are_rejected(self, site):
        broken = {**ACTION, "parameters": {**ACTION["parameters"], "pod": {"pattern": "shop-[a-z"}}}
        found = self.errors(site, allow(actions=[dict(ACTION), broken]))
        assert any("action id 'restart-shop-pod' is used 2 times" in err for err in found)
        assert any("not a valid regular expression" in err for err in found)

    def test_a_rule_must_be_values_or_pattern_not_both(self, site):
        both = {**ACTION, "parameters": {**ACTION["parameters"], "pod": {"values": ["a"], "pattern": "a"}}}
        assert any(err.startswith("autonomy.yaml:") for err in self.errors(site, allow(actions=[both])))
