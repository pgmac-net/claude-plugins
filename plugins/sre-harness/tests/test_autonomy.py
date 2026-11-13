"""Tests for autonomy.py and the unauthorised-mutation report."""

from __future__ import annotations

import json
import os
import subprocess
import sys

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
SCRIPTS = audit.SCRIPTS_DIR


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


@pytest.mark.parametrize(
    "command",
    [
        "kubectl --context 'main' delete pod shop-7f9c-x2k4 -n shop",
        'kubectl --context main delete pod "shop-7f9c-x2k4" -n shop',
        "kubectl --context main delete pod shop-7f9c-x2k4 -n ~shop",
        "kubectl --context main delete pod shop-* -n shop",
        "kubectl --context main delete pod shop-{7f9c-x2k4,db-0} -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n sh\\op",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop #",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop !!",
        "kubectl --context main delete pod shop-7f9c-x2k? -n shop",
        "kubectl --context main delete pod shop-[7]f9c-x2k4 -n shop",
        "kubectl  --context main delete pod shop-7f9c-x2k4 -n shop",
        "kubectl --context main delete pod shop-7f9c-x2k4 -n shop ",
        "kubectl\t--context main delete pod shop-7f9c-x2k4 -n shop",
        "KUBECONFIG=/tmp/evil kubectl --context main delete pod shop-7f9c-x2k4 -n shop",
    ],
)
def test_anything_a_shell_would_read_differently_is_tier_one(command):
    """What is matched must be exactly what runs, so quoting and expansion characters are refused outright."""
    assert decide(command)["tier"] == 1


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


ECHO = {
    "id": "say",
    "service": "component:example-service",
    "command": "echo restarted {name}",
    "parameters": {"name": {"pattern": "[a-z0-9-]+"}},
    "precondition": "It is safe to say so.",
    "max_per_incident": 2,
}
EVIDENCE = ["--precondition-evidence", "observed CrashLoopBackOff; volume Ready"]


@pytest.mark.usefixtures("incident")
class TestRun:
    @pytest.fixture(autouse=True)
    def _allow(self, incident):
        actions = [dict(ECHO), {**ECHO, "id": "fail", "command": "false", "parameters": {}}]
        (incident / "autonomy.yaml").write_text(yaml.safe_dump(allow(actions=actions)), encoding="utf-8")

    def records(self) -> list[dict]:
        path = audit_hook.spool_path("catalog-5")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_run_executes_the_matched_argv_and_records_claim_and_result(self, capfd):
        assert autonomy.main(["run", "--command", "echo restarted shop-1", *EVIDENCE]) == 0
        out = capfd.readouterr().out
        assert '"ran": true' in out and out.rstrip().endswith("restarted shop-1")
        assert [(r["event"], r.get("exit_code")) for r in self.records()] == [("tier2", None), ("tier2_result", 0)]
        assert self.records()[0]["command"] == "echo restarted shop-1"
        assert self.records()[0]["precondition_evidence"].startswith("observed CrashLoopBackOff")

    def test_no_shell_is_involved(self, capfd, tmp_path):
        """Even if an unsafe string reached run, it is refused before execution; nothing is created."""
        target = tmp_path / "made-by-shell"
        assert autonomy.main(["run", "--command", f"echo restarted x; touch {target}", *EVIDENCE]) == autonomy.NOT_RUN
        assert not target.exists()
        assert self.records() == []
        assert '"tier": 1' in capfd.readouterr().out

    def test_run_needs_evidence_of_the_precondition(self, capfd):
        assert autonomy.main(["run", "--command", "echo restarted shop-1"]) == autonomy.NOT_RUN
        assert "no --precondition-evidence" in capfd.readouterr().out
        assert self.records() == []

    def test_commands_are_redacted_in_tier_two_records(self, incident):
        token = "abcdef0123456789abcd"
        rule = {"value": {"pattern": "[a-z0-9]+"}}
        action = {**ECHO, "id": "tok", "command": "echo --token={value}", "parameters": rule}
        (incident / "autonomy.yaml").write_text(yaml.safe_dump(allow(actions=[action])), encoding="utf-8")
        assert autonomy.main(["run", "--command", f"echo --token={token}", *EVIDENCE]) == 0
        assert token not in json.dumps(self.records())
        assert all("[REDACTED:" in r["command"] for r in self.records())

    def test_concurrent_runs_cannot_exceed_the_limit(self, incident):
        env = {
            "SRE_HARNESS_SITE": str(incident),
            "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
            "PATH": os.environ["PATH"],
        }
        command = [sys.executable, str(SCRIPTS / "autonomy.py"), "run", "--command", "echo restarted shop-1", *EVIDENCE]
        # Started together on purpose: the point is that they overlap.
        procs = [  # pylint: disable=consider-using-with
            subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(8)
        ]
        codes = [proc.wait() for proc in procs]
        assert sorted(codes) == [0, 0] + [autonomy.NOT_RUN] * 6
        assert sum(1 for r in self.records() if r["event"] == "tier2") == 2
        assert audit.verify(audit.read_log("catalog-5"))["chain_intact"] is True

    def test_the_commands_exit_code_is_returned_and_recorded(self):
        assert autonomy.main(["run", "--command", "false", *EVIDENCE]) == 1
        assert self.records()[-1] == {**self.records()[-1], "event": "tier2_result", "exit_code": 1}

    def test_the_limit_is_enforced_from_the_audit_log(self, capfd):
        for _ in range(2):
            assert autonomy.main(["run", "--command", "echo restarted shop-1", *EVIDENCE]) == 0
        assert autonomy.main(["run", "--command", "echo restarted shop-1", *EVIDENCE]) == autonomy.NOT_RUN
        assert "reached its limit of 2" in capfd.readouterr().out
        assert sum(1 for r in self.records() if r["event"] == "tier2") == 2

    def test_a_drill_never_runs_anything(self, capfd):
        state = sre_common.read_incident_state("catalog-5")
        sre_common.write_incident_state({**state, "drill": True})
        assert autonomy.main(["run", "--command", "echo restarted shop-1", *EVIDENCE]) == 2
        assert "a drill never mutates anything" in capfd.readouterr().err
        assert self.records() == []

    def test_match_changes_nothing(self, capfd):
        assert autonomy.main(["match", "--command", "echo restarted shop-1"]) == 0
        assert json.loads(capfd.readouterr().out)["tier"] == 2
        assert self.records() == []

    def test_site_without_an_allow_list_is_all_tier_one(self, incident, capfd):
        doc = yaml.safe_load((incident / "site.yaml").read_text())
        del doc["incident"]["autonomy_file"]
        (incident / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        assert autonomy.main(["match", "--command", "echo restarted shop-1"]) == 0
        assert json.loads(capfd.readouterr().out)["tier"] == 1


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
            f"uv run {SCRIPTS}/timeline.py add --type action --text 'deleted pod; rm of stale file'",
            f"uv run {SCRIPTS}/incident.py set --phase Mitigating",
            f"{SCRIPTS}/audit.py show",
            f"python3 {SCRIPTS}/timeline.py due",
        ],
    )
    def test_diagnostics_and_record_keeping_are_not(self, command):
        assert not audit.is_mutation(command)

    @pytest.mark.parametrize(
        "command",
        [
            f"uv run {SCRIPTS}/timeline.py add --type update --text ok; kubectl delete pod x -n shop",
            f"uv run {SCRIPTS}/audit.py show && sudo systemctl restart kubelet",
            f"uv run {SCRIPTS}/incident.py status | tee /etc/thing",
            "echo scripts/audit.py; rm -rf /var/lib/thing",
            "true # scripts/audit.py\nkubectl delete ns shop",
            f'uv run {SCRIPTS}/timeline.py add --text "$(kubectl delete pod x)"',
            "cat /p/scripts/audit.py > /dev/null; reboot",
        ],
    )
    def test_a_harness_call_does_not_excuse_what_is_chained_to_it(self, command):
        assert audit.is_mutation(command)

    @pytest.mark.parametrize(
        "command",
        [
            f"uv run --with evil-package {SCRIPTS}/audit.py record --kind note --text 'rm -rf x'",
            f"uv run --script /tmp/evil.py {SCRIPTS}/audit.py record --kind note --text 'rm -rf x'",
            "uv run /tmp/elsewhere/scripts/audit.py record --kind note --text 'kubectl delete ns shop'",
            "/tmp/scripts/timeline.py add --text 'sudo reboot'",
            f"uv run {SCRIPTS}/not_a_harness_script.py --do 'rm -rf x'",
            f"uv run {SCRIPTS}/redact.py --do 'rm -rf x'",
        ],
    )
    def test_only_a_plain_call_of_this_plugins_own_script_is_exempt(self, command):
        assert audit.is_mutation(command)

    def test_a_command_too_long_to_classify_counts_as_a_mutation(self):
        padded = "echo " + "a" * (audit.MAX_CLASSIFY + 10) + "; kubectl delete ns shop"
        assert audit.is_mutation(padded)
        assert audit.is_mutation("echo " + "a" * (audit.MAX_CLASSIFY + 10))

    def test_a_mutation_at_the_end_of_a_long_command_is_seen(self):
        assert audit.is_mutation("echo " + "a " * 5000 + "; kubectl delete ns shop")

    def test_unparseable_command_is_classified_by_its_text(self):
        assert audit.is_mutation("kubectl delete pod 'unterminated")
        assert not audit.is_mutation("kubectl get pod 'unterminated")

    def entries(self, *records: dict) -> list[tuple[str, dict]]:
        return [(json.dumps(r), {"ts": "2026-01-02T03:04:05Z", **r}) for r in records]

    def attempt(self, command: str) -> dict:
        return {"event": "attempt", "tool": "Bash", "capture": "hook", "input": {"command": command}}

    def test_mutation_with_no_authority_is_reported(self):
        found = audit.unauthorised_mutations(self.entries(self.attempt("kubectl get pods"), self.attempt(GOOD)))
        assert [item["command"] for item in found] == [GOOD]

    def test_approval_authorises_exactly_one_run_and_must_come_first(self):
        approval = {"event": "approval", "command": GOOD, "capture": "self-reported"}
        assert audit.unauthorised_mutations(self.entries(approval, self.attempt(GOOD))) == []
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

    @pytest.mark.parametrize("suffix", [" && reboot", " 'x'", " ~/x", " *", "  x", " $HOME"])
    def test_templates_outside_the_safe_character_set_are_rejected(self, site, suffix):
        bad = allow(actions=[{**ACTION, "command": ACTION["command"] + suffix}])
        assert any("no quoting, shell operators" in err for err in self.errors(site, bad))

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
