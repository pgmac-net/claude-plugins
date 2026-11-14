"""Tests for the audit hook, redaction and audit.py."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import audit
import audit_hook
import pytest
import redact
import sre_common
import timeline

PLUGIN = Path(__file__).resolve().parent.parent
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "sre-harness"


def call(event: str, tool: str = "Bash", tool_input=None, response=None, session: str = "s1") -> dict:
    payload = {"session_id": session, "hook_event_name": event, "tool_name": tool, "tool_input": tool_input or {}}
    if response is not None:
        payload["tool_response"] = response
    return payload


def open_incident(session: str = "s1", incident: str = "incidents-7") -> None:
    """What the hook sees when a session runs incident.py open: a bind token incident.py stored."""
    token = hashlib.sha256(f"{session}{incident}".encode()).hexdigest()[:32]
    binds = audit_hook.state_dir() / "binds"
    binds.mkdir(parents=True, exist_ok=True)
    (binds / token).write_text(incident + "\n", encoding="utf-8")
    audit_hook.handle(
        call(
            "PostToolUse",
            tool_input={"command": "uv run /x/scripts/incident.py open --title T --severity P2"},
            response={
                "stdout": json.dumps({"id": incident, "url": "https://example.com/7", "bind": token}),
                "stderr": "",
            },
            session=session,
        )
    )


def log(incident: str = "incidents-7") -> list[dict]:
    path = audit_hook.spool_path(incident)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


class TestRedaction:
    def test_known_token_shapes_are_replaced(self):
        out = redact.redact_text(f"curl -H 'Authorization: Bearer {TOKEN}' https://example.com")
        assert TOKEN not in out
        assert "[REDACTED:" in out

    @pytest.mark.parametrize(
        "text",
        [
            "mysql --password=hunter2hunter2 -e 'select 1'",
            "export API_KEY: abcdef123456",
            "https://admin:" + "hunter2hunter2@db.example.com/x",
        ],
    )
    def test_assignments_and_url_credentials_are_replaced(self, text):
        out = redact.redact_text(text)
        assert "hunter2hunter2" not in out and "abcdef123456" not in out
        assert "[REDACTED:" in out

    def test_command_shape_survives(self):
        out = redact.redact_text("mysql --password=hunter2hunter2 -e 'select 1'")
        assert out.startswith("mysql --password=[REDACTED:secret assignment]")
        assert out.endswith("-e 'select 1'")

    def test_environment_secret_values_are_replaced_by_name(self):
        env = {
            "GRAFANA_TOKEN": "s3cr3t-value-xyz",
            "SITE_CRED": "another-value-123",
            "PATH": "/usr/bin:/bin",
            "PIN": "1234",
        }
        values = redact.env_secrets(env, extra_names=["SITE_CRED"])
        assert set(values.values()) == {"GRAFANA_TOKEN", "SITE_CRED"}
        out = redact.redact_text("curl -u x:another-value-123 -H s3cr3t-value-xyz", values)
        assert out == "curl -u x:[REDACTED:env SITE_CRED] -H [REDACTED:env GRAFANA_TOKEN]"

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("mysql --password hunter2hunter2 -e 'select 1'", "hunter2hunter2"),
            ("tool --api-key abcdef123456 run", "abcdef123456"),
            ("curl -u shop:hunter2hunter2 https://example.com", "hunter2hunter2"),
            ("curl --user 'shop:hunter2 hunter2' https://example.com", "hunter2"),
            ("curl -H 'Authorization: Basic c2hvcDpodW50ZXIy' https://example.com", "c2hvcDpodW50ZXIy"),
            ("vault login -token-only --token s.AbCdEfGh1234", "s.AbCdEfGh1234"),
        ],
    )
    def test_flag_and_header_credentials_are_replaced(self, text, secret):
        out = redact.redact_text(text)
        assert secret not in out
        assert "[REDACTED:" in out

    def test_a_replacement_is_never_redacted_again(self):
        out = redact.redact_text("x --password hunter2hunter2 --token=abcdef123456 -u a:bcdefgh")
        assert out.count("[REDACTED:") == 3
        assert "REDACTED:REDACTED" not in out and "[REDACTED:[" not in out

    def test_file_content_is_stored_as_a_digest_only(self):
        secret_file = "db_password = hunter2hunter2\n" * 3
        out = redact.redact(
            {"file_path": "/etc/app.conf", "content": secret_file, "new_string": "x", "replace_all": True}
        )
        assert out["file_path"] == "/etc/app.conf" and out["replace_all"] is True
        assert out["content"] == {
            "omitted": "content",
            "bytes": len(secret_file.encode()),
            "sha256": hashlib.sha256(secret_file.encode()).hexdigest(),
        }
        assert out["new_string"]["omitted"] == "content"

    def test_huge_input_is_bounded_before_any_pattern_runs(self):
        nasty = ("password" * 20000) + ("=" * 20000) + ("-----BEGIN " + "PRIVATE KEY-----" + "A" * 200000)
        started = time.monotonic()
        out = redact.redact_text(nasty)
        assert time.monotonic() - started < 1.0
        assert len(out) < 2300

    def test_long_strings_are_truncated_with_a_digest(self):
        out = redact.redact_text("a" * 5000)
        assert len(out) < 2200
        assert "truncated; original 5000 chars" in out
        assert hashlib.sha256(("a" * 5000).encode()).hexdigest() in out

    def test_nested_structures_keep_their_keys(self):
        out = redact.redact({"command": f"echo {TOKEN}", "args": [TOKEN, 3], "n": 1})
        assert set(out) == {"command", "args", "n"}
        assert TOKEN not in json.dumps(out)
        assert out["n"] == 1

    def test_plain_commands_are_untouched(self):
        text = "kubectl --context main get pods -n shop -o wide"
        assert redact.redact_text(text) == text


class TestHook:
    @pytest.mark.usefixtures("state")
    def test_nothing_is_written_outside_an_incident(self):
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}))
        audit_hook.handle(call("PostToolUse", tool_input={"command": "ls"}, response={"stdout": "x"}))
        assert not (audit_hook.state_dir() / "audit").exists()

    @pytest.mark.usefixtures("state")
    def test_opening_an_incident_binds_the_session_and_is_itself_recorded(self):
        open_incident()
        assert audit_hook.bound_incident("s1") == "incidents-7"
        records = log()
        assert len(records) == 1
        assert records[0]["event"] == "complete"
        assert records[0]["incident"] == "incidents-7"
        assert records[0]["prev"] == audit_hook.GENESIS

    @pytest.mark.usefixtures("state")
    def test_printing_an_id_or_an_unknown_token_binds_nothing(self):
        forged = {"stdout": json.dumps({"id": "incidents-7", "bind": "0" * 32})}
        audit_hook.handle(call("PostToolUse", tool_input={"command": "echo incident.py open"}, response=forged))
        assert audit_hook.bound_incident("s1") is None
        assert not (audit_hook.state_dir() / "audit").exists()

    @pytest.mark.usefixtures("state")
    def test_bind_token_is_single_use_and_the_id_comes_from_the_file(self):
        open_incident()
        assert list((audit_hook.state_dir() / "binds").iterdir()) == []
        token = hashlib.sha256(b"s1incidents-7").hexdigest()[:32]
        replay = {"stdout": json.dumps({"id": "incidents-999", "bind": token})}
        audit_hook.handle(call("PostToolUse", tool_input={"command": "x"}, response=replay, session="s9"))
        assert audit_hook.bound_incident("s9") is None

    @pytest.mark.usefixtures("state")
    def test_rebinding_a_live_session_is_recorded_in_the_log_it_leaves(self):
        open_incident()
        open_incident(incident="incidents-8")
        assert audit_hook.bound_incident("s1") == "incidents-8"
        left = log("incidents-7")[-1]
        assert (left["event"], left["to"]) == ("rebind", "incidents-8")
        assert audit.verify(audit.read_log("incidents-7"))["rebinds"] == 1

    @pytest.mark.usefixtures("state")
    def test_append_reads_only_the_tail_and_keeps_the_chain(self):
        open_incident()
        for index in range(40):
            audit_hook.handle(call("PreToolUse", tool_input={"command": f"echo {index} " + "x" * 3000}))
        assert audit.verify(audit.read_log("incidents-7"))["chain_intact"] is True
        assert audit_hook.head("incidents-7")[0] == 41

    @pytest.mark.usefixtures("state")
    def test_other_sessions_are_not_captured(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}, session="s2"))
        assert all(record["session"] == "s1" for record in log())

    @pytest.mark.usefixtures("state")
    def test_attempt_and_completion_with_output_hashed_not_stored(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": "kubectl get pods"}))
        response = {"stdout": "pod-a Running\nsecret-looking-output", "stderr": "", "interrupted": False}
        audit_hook.handle(call("PostToolUse", tool_input={"command": "kubectl get pods"}, response=response))
        attempt, complete = log()[1:]
        assert (attempt["event"], complete["event"]) == ("attempt", "complete")
        assert "output" not in attempt
        text = json.dumps(response, sort_keys=True)
        assert complete["output"] == {"bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}
        assert complete["outcome"] == {"interrupted": False}
        assert "secret-looking-output" not in json.dumps(log())

    @pytest.mark.usefixtures("state")
    def test_inputs_are_redacted(self, monkeypatch):
        monkeypatch.setenv("SHOP_DB_PASSWORD", "correct-horse-battery")
        open_incident()
        command = f"curl -H 'X-Token: {TOKEN}' -u shop:correct-horse-battery https://example.com"
        audit_hook.handle(call("PreToolUse", tool_input={"command": command, "description": "check"}))
        record = log()[-1]
        assert TOKEN not in json.dumps(record) and "correct-horse-battery" not in json.dumps(record)
        assert "[REDACTED:env SHOP_DB_PASSWORD]" in record["input"]["command"]
        assert record["input"]["description"] == "check"

    @pytest.mark.usefixtures("state")
    def test_non_bash_tools_are_recorded_too(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool="Edit", tool_input={"file_path": "/etc/x", "new_string": "y"}))
        assert log()[-1]["tool"] == "Edit"

    @pytest.mark.usefixtures("state")
    def test_capture_stops_once_the_log_is_closed(self):
        open_incident()
        audit.release_capture("incidents-7")
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}))
        assert len(log()) == 1

    def test_hook_process_never_fails_or_prints(self, state):
        for stdin in ("not json", "", json.dumps({"session_id": "s1", "hook_event_name": "PreToolUse"})):
            done = subprocess.run(
                [sys.executable, str(PLUGIN / "scripts" / "audit_hook.py")],
                input=stdin, capture_output=True, text=True, check=False,
                env={"XDG_STATE_HOME": str(state.parent), "PATH": "/usr/bin:/bin"},
            )  # fmt: skip
            assert (done.returncode, done.stdout, done.stderr) == (0, "", "")

    def test_hooks_manifest_points_at_the_script_and_guards_on_the_active_marker(self):
        manifest = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        assert set(manifest["hooks"]) == {"PreToolUse", "PostToolUse"}
        for entries in manifest["hooks"].values():
            command = entries[0]["hooks"][0]["command"]
            assert "sre-harness/active" in command and command.rstrip().endswith("|| true")
            assert "${CLAUDE_PLUGIN_ROOT}/scripts/audit_hook.py" in command
        assert (PLUGIN / "scripts" / "audit_hook.py").is_file()


class TestVerify:
    @pytest.mark.usefixtures("state")
    def test_intact_chain_and_summary(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}))
        audit_hook.handle(call("PostToolUse", tool_input={"command": "ls"}, response={"stdout": ""}))
        result = audit.verify(audit.read_log("incidents-7"))
        assert result["chain_intact"] is True
        assert result["events"] == {"attempt": 1, "complete": 2}
        assert result["attempts_without_completion"] == []

    @pytest.mark.usefixtures("state")
    def test_attempt_with_no_completion_is_reported(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": "kubectl delete pod x"}))
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}))
        audit_hook.handle(call("PostToolUse", tool_input={"command": "ls"}, response={"stdout": ""}))
        unfinished = audit.verify(audit.read_log("incidents-7"))["attempts_without_completion"]
        assert [record["input"]["command"] for record in unfinished] == ["kubectl delete pod x"]

    @pytest.mark.usefixtures("state")
    @pytest.mark.parametrize("tamper", ["edit", "delete"])
    def test_editing_or_deleting_a_line_breaks_the_chain(self, tamper):
        open_incident()
        for name in ("a", "b", "c"):
            audit_hook.handle(call("PreToolUse", tool_input={"command": name}))
        path = audit_hook.spool_path("incidents-7")
        lines = path.read_text(encoding="utf-8").splitlines()
        if tamper == "edit":
            lines[1] = lines[1].replace('"a"', '"z"')
        else:
            del lines[1]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = audit.verify(audit.read_log("incidents-7"))
        assert result["chain_intact"] is False
        assert result["broken_at_lines"]


class TestAuditCli:
    @pytest.fixture
    def incident(self, state, site, monkeypatch):
        monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
        sre_common.write_incident_state(
            {
                "id": "catalog-5",
                "repo": "example-org/sre-catalog",
                "number": 5,
                "url": "u",
                "title": "T",
                "redact_env": [],
                "status": "resolved",
            }
        )
        (state / "active").write_text("x\n", encoding="utf-8")
        open_incident(incident="catalog-5")
        return site

    @pytest.mark.usefixtures("incident")
    def test_record_adds_a_self_reported_redacted_entry(self):
        args = ["record", "--kind", "approval", "--text", f"Restart shop; blast radius one pod. token {TOKEN}"]
        assert audit.main([*args, "--command", "kubectl delete pod shop-1", "--approver", "owner"]) == 0
        record = log("catalog-5")[-1]
        assert (record["event"], record["capture"], record["approver"]) == ("approval", "self-reported", "owner")
        assert record["command"] == "kubectl delete pod shop-1"
        assert TOKEN not in record["text"]
        assert audit.verify(audit.read_log("catalog-5"))["self_reported"] == 1

    @pytest.mark.usefixtures("incident")
    def test_verify_exit_code_follows_the_chain(self, capsys):
        audit.main(["record", "--kind", "note", "--text", "second record"])
        assert audit.main(["verify"]) == 0
        path = audit_hook.spool_path("catalog-5")
        path.write_text(path.read_text(encoding="utf-8").replace("catalog-5", "catalog-6", 1), encoding="utf-8")
        assert audit.main(["--incident", "catalog-5", "verify"]) == 1
        assert '"chain_intact": false' in capsys.readouterr().out

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_commit_pushes_a_branch_and_stops_capture(self, incident, state, tmp_path, capsys):
        def git(*args, cwd=incident):
            subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        git("add", "-A")
        git("commit", "-m", "init")
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "main")

        assert audit.main(["commit", "--no-pr", "--offline"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["branch"] == "audit/catalog-5" and out["file"] == "audit/catalog-5.jsonl"
        shown = subprocess.run(
            ["git", "-C", str(remote), "show", "audit/catalog-5:audit/catalog-5.jsonl"],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
        assert shown.stdout == audit_hook.spool_path("catalog-5").read_text(encoding="utf-8")
        assert audit_hook.is_closed("catalog-5")
        assert not (state / "active").exists()
        status = subprocess.run(
            ["git", "-C", str(incident), "status", "--porcelain"], check=True, capture_output=True, text=True
        )
        assert status.stdout == ""
        assert audit.main(["record", "--kind", "note", "--text", "late"]) == 2

    @pytest.mark.usefixtures("incident")
    def test_commit_refuses_a_broken_chain(self, capsys):
        path = audit_hook.spool_path("catalog-5")
        path.write_text('{"tampered": true}\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
        assert audit.main(["commit", "--no-pr", "--offline"]) == 2
        assert "hash chain is broken" in capsys.readouterr().err


class TestHardening:
    @pytest.mark.usefixtures("state")
    def test_touching_the_state_directory_is_reported(self):
        open_incident()
        marker = str(audit_hook.state_dir() / "active")
        audit_hook.handle(call("PreToolUse", tool_input={"command": f"rm {marker}"}))
        audit_hook.handle(call("PreToolUse", tool_input={"command": "uv run p/scripts/audit.py show"}))
        found = audit.verify(audit.read_log("incidents-7"))["state_dir_access"]
        assert [item["input"]["command"] for item in found] == [f"rm {marker}"]

    @pytest.mark.usefixtures("state")
    def test_a_secret_that_survived_redaction_blocks_publication(self, site, monkeypatch, capsys):
        monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
        sre_common.write_incident_state({"id": "catalog-5", "repo": "o/r", "number": 5, "url": "u", "title": "T"})
        audit_hook.append("catalog-5", {"v": 1, "ts": "2026-01-02T03:04:05Z", "event": "note", "text": TOKEN})
        result = audit.verify(audit.read_log("catalog-5"))
        assert result["leftover_secrets"] == [{"line": 1, "looks_like": ["GitHub token"]}]
        assert audit.main(["commit", "--no-pr", "--offline"]) == 2
        err = capsys.readouterr().err
        assert "still look like they hold a secret" in err and TOKEN not in err

    @pytest.mark.usefixtures("state")
    def test_redacted_lines_are_not_mistaken_for_secrets(self):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": f"curl -H 'Authorization: Bearer {TOKEN}' x"}))
        assert audit.verify(audit.read_log("incidents-7"))["leftover_secrets"] == []

    @pytest.mark.usefixtures("state")
    def test_timeline_anchors_catch_a_rewritten_log(self, monkeypatch):
        open_incident()
        audit_hook.handle(call("PreToolUse", tool_input={"command": "kubectl delete pod x"}))
        count, head = audit_hook.head("incidents-7")
        pinned = [{"type": "action", "at": "2026-01-02T03:04:05Z", "audit_anchor": {"records": count, "head": head}}]
        monkeypatch.setattr(audit.timeline, "fetch", lambda _state, _responders=None: pinned)
        state_doc = {"id": "incidents-7"}
        assert audit.check_anchors(state_doc, audit.read_log("incidents-7"), None) == {
            "anchors_checked": 1,
            "anchor_mismatches": [],
        }
        # Rewrite the whole log without the deletion, recomputing the chain so it looks intact.
        path = audit_hook.spool_path("incidents-7")
        first = path.read_text(encoding="utf-8").splitlines()[0]
        path.write_text(first + "\n", encoding="utf-8")
        audit_hook.handle(call("PreToolUse", tool_input={"command": "ls"}))
        entries = audit.read_log("incidents-7")
        assert audit.verify(entries)["chain_intact"] is True
        problems = audit.check_anchors(state_doc, entries, None)["anchor_mismatches"]
        assert len(problems) == 1 and "not the one pinned" in problems[0]["problem"]

    def test_timeline_marker_carries_and_parses_the_anchor(self):
        moment = sre_common.parse_utc("2026-01-02T03:04:05Z")
        body = timeline.render("action", "did it", moment, "Etc/UTC", (12, "a" * 64))
        assert body.splitlines()[0].endswith(f"audit={'a' * 64} n=12 -->")
        entry = timeline.parse(body, "2026-01-02T03:05:00Z", "owner")
        assert entry["audit_anchor"] == {"records": 12, "head": "a" * 64}
        assert (
            timeline.parse(timeline.render("action", "x", moment, "Etc/UTC"), "2026-01-02T03:05:00Z", "o")["type"]
            == "action"
        )


class TestActiveMarker:
    """The marker switches capture on. It may go only when nothing is live or about to be."""

    @pytest.fixture(autouse=True)
    def _marker(self, state):
        state.mkdir(parents=True, exist_ok=True)
        (state / "active").write_text("x\n", encoding="utf-8")

    @property
    def state(self):
        return audit_hook.state_dir()

    def write_state(self, incident_id: str, status: str) -> None:
        sre_common.write_incident_state({"id": incident_id, "status": status}, make_current=False)

    def test_it_goes_when_everything_is_resolved_and_committed(self):
        self.write_state("incidents-1", "resolved")
        audit_hook.release_active_marker()
        assert not (self.state / "active").exists()

    def test_it_stays_for_an_incident_just_opened_that_has_no_log_yet(self):
        self.write_state("incidents-1", "resolved")
        self.write_state("incidents-2", "open")
        audit_hook.release_active_marker()
        assert (self.state / "active").exists()

    def test_it_stays_while_a_bind_token_waits_to_be_used(self):
        self.write_state("incidents-1", "resolved")
        (self.state / "binds").mkdir()
        (self.state / "binds" / ("c" * 32)).write_text("incidents-2\n", encoding="utf-8")
        audit_hook.release_active_marker()
        assert (self.state / "active").exists()

    def test_it_stays_while_a_log_is_open(self):
        audit_hook.append("incidents-3", {"v": 1, "ts": "2026-01-02T03:04:05Z", "event": "note"})
        audit_hook.release_active_marker()
        assert (self.state / "active").exists()

    def test_it_stays_when_a_state_file_cannot_be_read(self):
        (self.state / "incidents").mkdir()
        (self.state / "incidents" / "incidents-4.json").write_text("{not json", encoding="utf-8")
        audit_hook.release_active_marker()
        assert (self.state / "active").exists()

    def test_a_state_without_a_status_counts_as_open(self):
        sre_common.write_incident_state({"id": "incidents-5"}, make_current=False)
        audit_hook.release_active_marker()
        assert (self.state / "active").exists()

    def test_committing_one_incident_does_not_switch_capture_off_for_another(self):
        self.write_state("incidents-1", "resolved")
        self.write_state("incidents-2", "open")
        audit_hook.append("incidents-1", {"v": 1, "ts": "2026-01-02T03:04:05Z", "event": "note"})
        audit.release_capture("incidents-1")
        assert (self.state / "active").exists()
