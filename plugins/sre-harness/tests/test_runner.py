"""Unattended triage, script side: alert qualification, the private record and its promotion, the triage policy."""

from __future__ import annotations

import json
import shutil
import subprocess

import alerts
import audit
import audit_hook
import chat_policy
import incident
import pytest
import sre_common
import timeline
import validate_site
import yaml
from test_incident import FakeGh

NOW = 1_800_000_000
RUNNER = {
    "enabled": False,
    "source": "alerts",
    "tracker_repo": "catalog",
    "user_env": "NAGIOS_USER",
    "password_env": "NAGIOS_PASSWORD",
}


def service(name: str, tier: int, depends: list[str]) -> dict:
    return {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Component",
        "metadata": {"name": name},
        "spec": {
            "type": "service",
            "lifecycle": "production",
            "owner": "user:example-owner",
            "dependsOn": depends,
            "sre": {"tier": tier, "purpose": "p", "pattern": "example-pattern"},
        },
    }


def monitored(name: str, host: str) -> dict:
    return {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Resource",
        "metadata": {"name": name, "annotations": {"sre-harness/generated-by": "checks"}},
        "spec": {
            "type": "monitored-host",
            "owner": "user:example-owner",
            "sre": {"monitoring": {"source": "checks", "host": host, "checks": []}},
        },
    }


@pytest.fixture
def runner_site(edit_site):
    site = edit_site(lambda doc: doc.update({"runner": dict(RUNNER)}))
    catalog = site / "catalog"
    (catalog / "generated" / "checks").mkdir(parents=True)
    for name, host in (("monitored-web", "web01"), ("monitored-lab", "lab01")):
        (catalog / "generated" / "checks" / f"{name}.yaml").write_text(yaml.safe_dump(monitored(name, host)))
    (catalog / "services" / "shop.yaml").write_text(yaml.safe_dump(service("shop", 1, ["resource:monitored-web"])))
    toy = service("toy", 3, ["resource:monitored-lab"])
    # Hand-written services list checks as notes; the index must not mistake them for a monitored host.
    toy["spec"]["sre"]["monitoring"] = ["lab01: Disk"]
    (catalog / "services" / "toy.yaml").write_text(yaml.safe_dump(toy))
    return site


def status(code: int, since_s: int, **extra) -> dict:
    return {
        "status": code,
        "state_type": 1,
        "problem_has_been_acknowledged": False,
        "scheduled_downtime_depth": 0,
        "notifications_enabled": True,
        "last_state_change": since_s * 1000,
        "plugin_output": "CRITICAL - connection refused",
        **extra,
    }


class TestParse:
    def test_only_unhandled_hard_problems(self):
        services = {
            "servicelist": {
                "web01": {
                    "HTTP": status(16, NOW - 600),
                    "Soft": status(16, NOW - 600, state_type=0),
                    "Acked": status(16, NOW - 600, problem_has_been_acknowledged=True),
                    "Down": status(16, NOW - 600, scheduled_downtime_depth=1),
                    "Muted": status(4, NOW - 600, notifications_enabled=False),
                    "Fine": status(2, NOW - 600),
                }
            }
        }
        found = alerts.parse({}, services)
        assert [(a.target, a.state, a.since) for a in found] == [("web01/HTTP", "CRITICAL", NOW - 600)]

    def test_a_down_host_hides_its_own_checks(self):
        hosts = {"hostlist": {"web01": status(4, NOW - 900)}}
        services = {"servicelist": {"web01": {"HTTP": status(16, NOW - 600)}, "lab01": {"Disk": status(4, NOW)}}}
        assert [a.target for a in alerts.parse(hosts, services)] == ["web01", "lab01/Disk"]

    def test_line_cuts_untrusted_output(self):
        alert = alerts.Alert("web01", "HTTP", "CRITICAL", "x" * 1000 + "\nignore previous", NOW, ("component:shop",))
        assert len(alert.line()) < 400 and "\n" not in alert.line()
        assert alert.key == f"web01/HTTP@{NOW}"


class TestSelect:
    def index(self, runner_site):
        return alerts.host_services(runner_site, sre_common.load_yaml(runner_site / "site.yaml"))

    def test_catalog_maps_hosts_to_claiming_services(self, runner_site):
        index = self.index(runner_site)
        assert index["web01"] == [("component:shop", 1)]
        assert index["lab01"] == [("component:toy", 3)]

    def test_tier_age_seen_and_cooldown(self, runner_site):
        index = self.index(runner_site)
        settings = alerts.settings({"runner": RUNNER})
        old = alerts.Alert("web01", "HTTP", "CRITICAL", "o", NOW - 600)
        fresh = alerts.Alert("web01", "Ping", "CRITICAL", "o", NOW - 30)
        hobby = alerts.Alert("lab01", "Disk", "WARNING", "o", NOW - 600)
        unknown = alerts.Alert("nowhere", None, "DOWN", "o", NOW - 600)
        picked = alerts.select([old, fresh, hobby, unknown], index, settings, seen=set(), cooldowns={}, now=NOW)
        assert [(a.target, a.services) for a in picked.start] == [("web01/HTTP", ("component:shop",))]
        assert "too recent" in picked.skipped[fresh.key]
        assert "tier" in picked.skipped[hobby.key] and "tier" in picked.skipped[unknown.key]
        assert alerts.select([old], index, settings, seen={old.key}, cooldowns={}, now=NOW).start == []
        cooling = alerts.select([old], index, settings, seen=set(), cooldowns={"component:shop": NOW - 60}, now=NOW)
        assert cooling.start == [] and "cooling down" in cooling.skipped[old.key]
        later = alerts.select([old], index, settings, seen=set(), cooldowns={"component:shop": NOW - 3600}, now=NOW)
        assert len(later.start) == 1

    def test_status_url_and_credentials(self, runner_site):
        doc = sre_common.load_yaml(runner_site / "site.yaml")
        assert alerts.status_url(doc) == "https://monitoring.example.com/cgi-bin/statusjson.cgi"
        assert alerts.credentials(doc, {"NAGIOS_USER": "u", "NAGIOS_PASSWORD": "p"}) == ("u", "p")
        with pytest.raises(sre_common.SiteError):
            alerts.credentials(doc, {"NAGIOS_USER": "u"})


CHAT = {
    "adapter": "slack",
    "hub_channel": "C" + "0123456789",
    "responders": ["U" + "0123456789"],
    "approvers": ["U" + "0123456789"],
    "secrets_env": {"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"},
}


class TestValidate:
    def test_a_good_runner_validates(self, runner_site):
        path = runner_site / "site.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["chat"] = CHAT
        path.write_text(yaml.safe_dump(doc))
        report = validate_site.validate(runner_site)
        assert not [e for e in report.errors if "runner" in e], report.errors
        assert report.summary["runner"]["tracker_repo"] == "catalog"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"tracker_repo": "incidents"}, "not private"),
            ({"source": "metrics"}, "only nagios"),
            ({"source": "nope"}, "not a monitoring source"),
            ({"password_env": None}, "go together"),
        ],
    )
    def test_bad_runner_settings(self, edit_site, change, message):
        def mutate(doc):
            runner = {**RUNNER, **change}
            doc["runner"] = {k: v for k, v in runner.items() if v is not None}
            doc["repos"]["incidents"]["visibility"] = "public"

        errors = validate_site.validate(edit_site(mutate)).errors
        assert any(message in e for e in errors), errors

    def test_runner_needs_chat(self, runner_site):
        assert any("needs a chat section" in e for e in validate_site.validate(runner_site).errors)


class PromoteGh(FakeGh):  # pylint: disable=too-few-public-methods
    """FakeGh plus closing issues and editing labels."""

    def __init__(self):
        super().__init__()
        self.closed: list[tuple[str, str]] = []

    def __call__(self, args, stdin=None, timeout=None):
        if args[:2] == ["issue", "close"]:
            self.closed.append((args[2], args[args.index("--reason") + 1]))
            return ""
        if args[:2] == ["issue", "edit"] and "--add-label" in args:
            self.calls.append(args)
            return ""
        if args[:2] == ["issue", "create"]:
            self.next_number += 1
        return super().__call__(args, stdin, timeout)


@pytest.fixture
def gh(monkeypatch, tmp_path, runner_site):
    fake = PromoteGh()
    monkeypatch.setattr(sre_common, "run_gh", fake)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SRE_HARNESS_SITE", str(runner_site))
    return fake


def open_auto(capsys, *extra) -> dict:
    assert incident.main(["open", "--auto", *extra, "--title", "Shop is down", "--severity", "P2"]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestPrivateRecord:
    def test_auto_opens_in_the_runner_tracker_with_only_the_auto_label(self, gh, capsys):
        opened = open_auto(capsys)
        assert opened["id"].startswith("catalog-")
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        assert create[create.index("--repo") + 1] == "example-org/sre-catalog"
        assert [a for a in create if a.startswith("--label")] == ["--label=auto-triage"]
        assert sre_common.read_incident_state(opened["id"])["auto"] is True

    def test_auto_lists_people_incidents_too_and_says_not_to_attach(self, gh, capsys):
        gh.open_issues = [{"number": 3, "title": "Web slow", "url": "u"}]
        assert incident.main(["open", "--auto", "--title", "T", "--severity", "P3"]) == 3
        out = capsys.readouterr().out
        assert "incidents-3" in out and "catalog-3" in out and "cannot attach" in out

    def test_auto_drill_goes_to_the_drill_tracker_with_both_labels(self, gh, capsys):
        open_auto(capsys, "--drill")
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        assert sorted(a for a in create if a.startswith("--label")) == ["--label=auto-triage", "--label=drill"]

    def test_promote_opens_public_issue_carries_conclusions_and_chains_logs(self, gh, capsys):
        private = open_auto(capsys)
        state = sre_common.read_incident_state(private["id"])
        audit_hook.append(private["id"], {"v": 1, "event": "note", "text": "evidence gathered"})
        timeline.post(state, "discovery", "raw evidence: pod logs", "UTC", None)
        timeline.post(state, "summary", "ingress down; propose restart", "UTC", None)
        assert incident.main(["promote", "--by", "Owner"]) == 0
        promoted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        public = sre_common.read_incident_state(promoted["id"])
        assert promoted["id"].startswith("incidents-") and public["promoted_from"] == private["id"]
        posted = [c["body"] for c in gh.comments]
        assert any("(from triage) ingress down" in body for body in posted)
        assert not any("(from triage) raw evidence" in body for body in posted)
        first = json.loads(audit_hook.spool_path(promoted["id"]).read_text().splitlines()[0])
        assert first["event"] == "promoted_from" and first["source"] == private["id"]
        assert first["source_head"] == audit_hook.head(private["id"])[1]
        assert (str(state["number"]), "completed") in gh.closed
        after = sre_common.read_incident_state(private["id"])
        assert after["promoted_to"] == promoted["id"] and after["status"] == "resolved"
        assert incident.main(["promote", "--incident", private["id"], "--by", "Owner"]) == 2

    @pytest.mark.usefixtures("gh")
    def test_a_drill_is_never_promoted_and_people_incidents_are_not_dismissed(self, capsys):
        open_auto(capsys, "--drill")
        assert incident.main(["promote", "--by", "Owner"]) == 2
        assert "never promoted" in capsys.readouterr().err
        assert incident.main(["open", "--new", "--title", "T", "--severity", "P3"]) == 0
        assert incident.main(["dismiss", "--by", "Owner", "--reason", "noise"]) == 2
        assert "not an unattended triage" in capsys.readouterr().err

    def test_dismiss_labels_closes_and_resolves(self, gh, capsys):
        opened = open_auto(capsys)
        assert incident.main(["dismiss", "--by", "Owner", "--reason", "flapping check"]) == 0
        number = str(sre_common.read_incident_state(opened["id"])["number"])
        assert (number, "not planned") in gh.closed
        assert any("--add-label" in c and "not-an-incident" in c for c in gh.calls)
        assert sre_common.read_incident_state(opened["id"])["dismissed"] is True


class TestTriagePolicy:
    def test_a_triage_session_writes_only_to_the_open_record_it_created(self, gh, capsys, monkeypatch):
        assert incident.main(["open", "--title", "People are on it", "--severity", "P2"]) == 0
        person = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["id"]
        monkeypatch.setenv(sre_common.TRIAGE_ENV, "cd" * 16)
        other = open_auto(capsys, "--new")["id"]
        monkeypatch.setenv(sre_common.TRIAGE_ENV, "ab" * 16)
        own = open_auto(capsys, "--new")["id"]
        assert sre_common.read_incident_state(own)["triage_token"] == "ab" * 16
        comments = len(gh.comments)
        for target in (person, other):
            assert timeline.main(["--incident", target, "add", "--type", "discovery", "--text", "x"]) == 2
            assert "unattended triage" in capsys.readouterr().err
            assert incident.main(["set", "--incident", target, "--phase", "Diagnosing"]) == 2
        assert len(gh.comments) == comments
        assert timeline.main(["--incident", own, "add", "--type", "discovery", "--text", "ok"]) == 0
        assert incident.main(["set", "--incident", own, "--phase", "Diagnosing"]) == 0
        state = sre_common.read_incident_state(own)
        sre_common.write_incident_state({**state, "dismissed": True}, make_current=False)
        assert timeline.main(["--incident", own, "add", "--type", "discovery", "--text", "late"]) == 2
        monkeypatch.setenv(sre_common.TRIAGE_ENV, "not-a-token")
        assert incident.main(["set", "--incident", other, "--phase", "Diagnosing"]) == 2

    def test_a_triage_session_cannot_open_on_the_public_tracker(self, gh, capsys, monkeypatch):
        monkeypatch.setenv(sre_common.TRIAGE_ENV, "ab" * 16)
        assert incident.main(["open", "--title", "Shop is down", "--severity", "P2"]) == 2
        assert "--auto" in capsys.readouterr().err
        assert not any(call[:2] == ["issue", "create"] for call in gh.calls)
        assert open_auto(capsys)["id"].startswith("catalog-")

    def test_triage_withholds_record_changes_and_allows_only_auto_open(self, tmp_path):
        rules = chat_policy.triage_rules({}, tmp_path)
        script = tmp_path / "incident.py"
        assert f"Bash(uv run {script} open --auto *)" in rules["allow"]
        assert f"Bash(uv run {script} open *)" not in rules["allow"]
        assert f"Bash(uv run {script} open *)" not in rules["deny"]
        for call in ("attach", "close", "promote", "dismiss"):
            assert f"Bash(uv run {script} {call} *)" in rules["deny"]
            assert f"Bash(uv run {script} {call} *)" not in rules["allow"]
        assert f"Bash(uv run {tmp_path / 'autonomy.py'} run *)" in rules["deny"]
        assert f"Bash(uv run {tmp_path / 'timeline.py'} *)" in rules["allow"]

    def test_chat_policy_allows_promote_and_dismiss(self, tmp_path):
        allow = chat_policy.rules({}, tmp_path)["allow"]
        assert f"Bash(uv run {tmp_path / 'incident.py'} promote *)" in allow
        assert json.loads(chat_policy.settings_json({}, tmp_path, triage=True))["permissions"]["deny"]


@pytest.mark.usefixtures("gh")
class TestGatewayRecords:
    def test_refusal_names_no_approver_and_policy_change_is_recorded(self, capsys):
        opened = open_auto(capsys)
        refused = audit.ChatDecision("refused", "Bash", {"command": "kubectl delete pod x"}, "t1", None, None, "triage")
        assert audit.record_chat_decision(opened["id"], refused)["event"] == "denial"
        with pytest.raises(ValueError, match="no approver"):
            audit.record_chat_decision(opened["id"], audit.ChatDecision("refused", "Bash", {}, "t2", "Owner", None))
        audit.record_policy_change(
            audit.PolicyChange(opened["id"], "s1", "triage-only", "chat", "Owner (U1)", "https://chat/x")
        )
        last = audit.read_log(opened["id"])[-1][1]
        assert (last["event"], last["from"], last["to"]) == ("policy_change", "triage-only", "chat")
        assert last["capture"] == "gateway"
        with pytest.raises(ValueError):
            audit.record_policy_change(audit.PolicyChange(opened["id"], "s1", "chat", "triage-only", "Owner", None))
        assert audit.verify(audit.read_log(opened["id"]))["chain_intact"]

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_commit_of_a_promoted_incident_takes_the_triage_log_too(self, runner_site, capsys):
        private = open_auto(capsys)
        audit_hook.append(private["id"], {"v": 1, "event": "note", "text": "triage"})
        assert incident.main(["promote", "--by", "Owner"]) == 0
        public = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        def git(*args, cwd=runner_site):
            subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

        remote = runner_site.parent / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        for args in (
            ("init", "-b", "main"),
            ("config", "user.email", "t@example.com"),
            ("config", "user.name", "T"),
            ("add", "-A"),
            ("commit", "-m", "init"),
            ("remote", "add", "origin", str(remote)),
            ("push", "-u", "origin", "main"),
        ):
            git(*args)
        assert audit.main(["commit", "--no-pr", "--offline"]) == 0
        listed = subprocess.run(
            ["git", "-C", str(remote), "ls-tree", "--name-only", f"audit/{public['id']}", "audit/"],
            check=True, capture_output=True, text=True,
        ).stdout.split()  # fmt: skip
        assert f"audit/{private['id']}.jsonl" in listed and f"audit/{public['id']}.jsonl" in listed
        assert audit_hook.is_closed(private["id"]) and audit_hook.is_closed(public["id"])
