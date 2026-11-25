"""Tests for state_gc.py: clearing local incident state only when the audit log is preserved."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import audit_hook
import incident
import pytest
import sre_common
import state_gc
import timeline
import yaml


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class World:
    """A site repository with a remote, a local state directory, and a stand-in for the code host."""

    def __init__(self, site: Path, state: Path, remote: Path, monkeypatch):
        self.site, self.state, self.remote = site, state, remote
        self.issues: dict[int, str] = {}
        self.gh_down = False
        monkeypatch.setattr(sre_common, "run_gh", self._gh)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        git(site, "init", "-b", "main")
        git(site, "config", "user.email", "t@example.com")
        git(site, "config", "user.name", "T")
        git(site, "add", "-A")
        git(site, "commit", "-m", "init")
        git(site, "remote", "add", "origin", str(remote))
        git(site, "push", "-u", "origin", "main")

    def _gh(self, args, stdin=None, timeout=None):  # noqa: ARG002  pylint: disable=unused-argument
        if self.gh_down:
            raise sre_common.SiteError("gh issue view failed: network unreachable")
        number = int(args[2])
        return self.issues.get(number, "OPEN") + "\n"

    def incident(self, number: int, *, committed: bool = True, resolved: bool = True, records: int = 3) -> str:
        """Local state for incidents-<number>, as the harness leaves it."""
        incident_id = f"incidents-{number}"
        for index in range(records):
            audit_hook.append(incident_id, {"v": 1, "ts": f"2026-01-02T03:04:0{index}Z", "event": "note", "n": index})
        sre_common.write_incident_state(
            {
                "id": incident_id,
                "repo": "example-org/incidents",
                "number": number,
                "url": "u",
                "title": "T",
                "status": "resolved" if resolved else "open",
            },
            make_current=False,
        )
        if committed:
            audit_hook.spool_path(incident_id).with_suffix(".closed").write_text("committed\n", encoding="utf-8")
        (self.state / "sessions").mkdir(parents=True, exist_ok=True)
        (self.state / "sessions" / f"s{number}.json").write_text(
            json.dumps({"incident": incident_id}), encoding="utf-8"
        )
        (self.state / "binds").mkdir(parents=True, exist_ok=True)
        (self.state / "binds" / f"{number:032x}").write_text(incident_id + "\n", encoding="utf-8")
        return incident_id

    def merge_audit(self, incident_id: str, content: bytes | None = None) -> None:
        """Land the audit file on the remote's default branch, as a merged pull request would."""
        target = self.site / "audit" / f"{incident_id}.jsonl"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content if content is not None else audit_hook.spool_path(incident_id).read_bytes())
        git(self.site, "add", "-A")
        git(self.site, "commit", "-m", f"audit: {incident_id}")
        git(self.site, "push", "origin", "main")

    def files(self) -> set[str]:
        return {str(p.relative_to(self.state)) for p in self.state.rglob("*") if p.is_file()}


@pytest.fixture
def world(site, tmp_path, monkeypatch) -> World:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
    (state_home / "sre-harness").mkdir(parents=True)
    return World(site, state_home / "sre-harness", tmp_path / "remote.git", monkeypatch)


def run_gc(world: World, *, apply: bool = True, **policy) -> dict:
    root, doc = sre_common.load_site(str(world.site))
    return state_gc.run(doc, root, state_gc.Policy(fetch=False, **policy), apply=apply)


def test_a_merged_closed_incident_leaves_no_file_behind(world):
    incident_id = world.incident(7)
    world.merge_audit(incident_id)
    world.issues[7] = "CLOSED"
    (world.state / "active").write_text("x\n", encoding="utf-8")
    (world.state / "current").write_text(incident_id + "\n", encoding="utf-8")

    result = run_gc(world)

    assert result["cleared"] == [incident_id] and result["kept"] == []
    assert world.files() == {f"retired/{incident_id}.json"}, "only the tombstone stays (homelabia#281)"


def test_the_merged_file_in_the_repository_is_never_touched(world):
    incident_id = world.incident(7)
    world.merge_audit(incident_id)
    world.issues[7] = "CLOSED"
    run_gc(world)
    assert (world.site / "audit" / f"{incident_id}.jsonl").is_file()


def test_dry_run_deletes_nothing_and_says_what_it_would(world):
    incident_id = world.incident(7)
    world.merge_audit(incident_id)
    world.issues[7] = "CLOSED"
    before = world.files()
    result = run_gc(world, apply=False)
    assert result["dry_run"] is True and result["cleared"] == [incident_id] and result["deleted"] == 0
    assert result["files"] >= 5
    assert world.files() == before


class TestWhatIsKept:
    def test_an_incident_whose_log_was_never_committed_is_the_only_copy(self, world):
        incident_id = world.incident(7, committed=False)
        world.issues[7] = "CLOSED"
        result = run_gc(world)
        assert result["cleared"] == []
        assert "only copy" in result["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_an_open_incident_is_kept_even_with_a_closed_marker(self, world):
        world.incident(7, resolved=False)
        world.issues[7] = "CLOSED"
        assert "not resolved" in run_gc(world)["kept"][0]["reason"]

    def test_committed_but_not_yet_merged_is_kept(self, world):
        incident_id = world.incident(7)
        world.issues[7] = "CLOSED"
        result = run_gc(world)
        assert "not on origin/main yet" in result["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_a_merged_file_that_differs_is_kept(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id, content=b'{"something": "else"}\n')
        world.issues[7] = "CLOSED"
        assert "differs from the local spool" in run_gc(world)["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_an_open_tracking_issue_keeps_it(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "OPEN"
        assert "still open" in run_gc(world)["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_an_unreachable_code_host_keeps_it(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.gh_down = True
        assert "could not confirm" in run_gc(world)["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_a_missing_audit_checkout_keeps_it(self, world):
        incident_id = world.incident(7)
        world.issues[7] = "CLOSED"
        doc = yaml.safe_load((world.site / "site.yaml").read_text())
        doc["audit"] = {"repo": "incidents", "path": "audit"}
        (world.site / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        result = run_gc(world)
        assert result["cleared"] == [] and "no local checkout" in result["kept"][0]["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_each_incident_is_judged_alone(self, world):
        done, pending = world.incident(7), world.incident(8)
        world.merge_audit(done)
        world.issues[7] = "CLOSED"
        world.issues[8] = "CLOSED"
        result = run_gc(world)
        assert result["cleared"] == [done]
        assert [item["id"] for item in result["kept"]] == [pending]
        assert audit_hook.spool_path(pending).is_file() and not audit_hook.spool_path(done).exists()


class TestFilesAndPointers:
    def test_only_this_incidents_sessions_and_tokens_go(self, world):
        done, other = world.incident(7), world.incident(8, committed=False)
        world.merge_audit(done)
        world.issues[7] = "CLOSED"
        run_gc(world)
        remaining = world.files()
        assert "sessions/s8.json" in remaining and f"binds/{8:032x}" in remaining
        assert "sessions/s7.json" not in remaining and f"binds/{7:032x}" not in remaining
        assert audit_hook.spool_path(other).is_file()

    def test_the_current_pointer_is_cleared_only_if_it_names_the_incident(self, world):
        done, other = world.incident(7), world.incident(8, committed=False)
        world.merge_audit(done)
        world.issues[7] = "CLOSED"
        (world.state / "current").write_text(other + "\n", encoding="utf-8")
        run_gc(world)
        assert (world.state / "current").read_text().strip() == other
        (world.state / "current").write_text(done + "\n", encoding="utf-8")
        audit_hook.retired_path(done).unlink()  # as attach --reopen would
        world.incident(7)
        run_gc(world)
        assert not (world.state / "current").exists()

    def test_the_active_marker_stays_while_a_log_is_still_open(self, world):
        done = world.incident(7)
        world.incident(8, committed=False)
        world.merge_audit(done)
        world.issues[7] = "CLOSED"
        (world.state / "active").write_text("x\n", encoding="utf-8")
        run_gc(world)
        assert (world.state / "active").exists()

    def test_clearing_an_old_incident_does_not_stop_capture_for_a_new_one(self, world):
        done = world.incident(7)
        world.merge_audit(done)
        world.issues[7] = "CLOSED"
        sre_common.write_incident_state(
            {"id": "incidents-9", "repo": "example-org/incidents", "number": 9, "status": "open"}, make_current=False
        )
        (world.state / "active").write_text("x\n", encoding="utf-8")
        assert run_gc(world)["cleared"] == [done]
        assert (world.state / "active").exists(), "incident 9 has just opened and its log has not started"

    def test_unused_bind_tokens_expire(self, world):
        binds = world.state / "binds"
        binds.mkdir(parents=True)
        old, fresh = binds / ("a" * 32), binds / ("b" * 32)
        old.write_text("incidents-1\n")
        fresh.write_text("incidents-2\n")
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        assert run_gc(world)["stale_tokens"] == 1
        assert not old.exists() and fresh.exists()

    def test_directories_and_odd_names_are_left_alone(self, world):
        audit_dir = world.state / "audit"
        audit_dir.mkdir(parents=True)
        (audit_dir / "notes.txt").write_text("keep\n")
        (audit_dir / "../outside.jsonl").parent.mkdir(exist_ok=True)
        (audit_dir / "Not An Id.jsonl").write_text("keep\n")
        nested = audit_dir / "incidents-9.jsonl"
        nested.mkdir()
        (nested / "inner").write_text("keep\n")
        run_gc(world)
        assert (audit_dir / "notes.txt").exists() and (audit_dir / "Not An Id.jsonl").exists()
        assert (nested / "inner").exists(), "a directory is never removed, and never recursed into"

    def test_a_symlink_is_removed_not_followed(self, world, tmp_path):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "CLOSED"
        precious = tmp_path / "precious.txt"
        precious.write_text("keep\n")
        pointer = world.state / "sessions" / "s7.json"
        pointer.unlink()
        pointer.symlink_to(precious)
        run_gc(world)
        assert precious.read_text() == "keep\n"


class TestAgeInsteadOfIssueState:
    """At incident start nothing can ask the code host, so age stands in for 'the issue is closed'."""

    def test_too_young_is_kept(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        result = run_gc(world, require_issue_closed=False, min_age_days=14)
        assert result["cleared"] == [] and "waiting for 14" in result["kept"][0]["reason"]

    def test_old_enough_is_cleared_without_asking_the_code_host(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.gh_down = True
        past = time.time() - 15 * 86400
        os.utime(audit_hook.spool_path(incident_id).with_suffix(".closed"), (past, past))
        assert run_gc(world, require_issue_closed=False, min_age_days=14)["cleared"] == [incident_id]

    def test_age_never_overrides_the_data_checks(self, world):
        incident_id = world.incident(7)
        past = time.time() - 90 * 86400
        os.utime(audit_hook.spool_path(incident_id).with_suffix(".closed"), (past, past))
        result = run_gc(world, require_issue_closed=False, min_age_days=14)
        assert result["cleared"] == [] and "not on origin/main" in result["kept"][0]["reason"]


def test_gc_fetches_so_a_merge_made_elsewhere_is_seen(world, tmp_path):
    incident_id = world.incident(7)
    world.issues[7] = "CLOSED"
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(world.remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "t@example.com")
    git(other, "config", "user.name", "T")
    (other / "audit").mkdir(exist_ok=True)
    (other / "audit" / f"{incident_id}.jsonl").write_bytes(audit_hook.spool_path(incident_id).read_bytes())
    git(other, "add", "-A")
    git(other, "commit", "-m", "audit")
    git(other, "push", "origin", "main")

    root, doc = sre_common.load_site(str(world.site))
    stale = state_gc.run(doc, root, state_gc.Policy(fetch=False), apply=False)
    assert stale["cleared"] == [], "without a fetch the merge is not yet known"
    fresh = state_gc.run(doc, root, state_gc.Policy(fetch=True), apply=False)
    assert fresh["cleared"] == [incident_id]


class TestCommandLine:
    def run(self, capsys, *argv: str) -> dict:
        assert incident.main(["gc", *argv]) == 0
        out = capsys.readouterr().out
        return json.loads(out) if out.strip() else {}

    def test_default_is_a_dry_run(self, world, capsys):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "CLOSED"
        result = self.run(capsys, "--no-fetch")
        assert result["dry_run"] is True and audit_hook.spool_path(incident_id).is_file()

    def test_apply_deletes_and_quiet_prints_nothing(self, world, capsys):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "CLOSED"
        assert self.run(capsys, "--no-fetch", "--apply", "--quiet") == {}
        assert world.files() == {f"retired/{incident_id}.json"}

    def test_ignore_issue_state_skips_only_the_issue_check(self, world, capsys):
        done, unmerged = world.incident(7), world.incident(8)
        world.merge_audit(done)
        result = self.run(capsys, "--no-fetch", "--ignore-issue-state")
        assert result["cleared"] == [done] and [k["id"] for k in result["kept"]] == [unmerged]


class TestTargeted:
    """gc --incident clears only the incidents named (homelabia#266)."""

    def run(self, capsys, *argv: str) -> dict:
        assert incident.main(["gc", "--no-fetch", *argv]) == 0
        return json.loads(capsys.readouterr().out)

    def test_only_the_named_incident_is_assessed_and_cleared(self, world, capsys):
        named, other = world.incident(7), world.incident(8)
        for incident_id in (named, other):
            world.merge_audit(incident_id)
        world.issues.update({7: "CLOSED", 8: "CLOSED"})
        result = self.run(capsys, "--apply", "--incident", named)
        assert result["cleared"] == [named] and result["kept"] == []
        assert audit_hook.spool_path(other).is_file() and sre_common.incident_state_path(other).is_file()
        assert not audit_hook.spool_path(named).exists()

    def test_the_flag_repeats(self, world, capsys):
        ids = [world.incident(7), world.incident(8), world.incident(9)]
        for incident_id in ids:
            world.merge_audit(incident_id)
        world.issues.update({7: "CLOSED", 8: "CLOSED", 9: "CLOSED"})
        result = self.run(capsys, "--incident", ids[0], "--incident", ids[2])
        assert result["cleared"] == [ids[0], ids[2]]

    @pytest.mark.usefixtures("world")
    def test_an_unknown_id_is_kept_with_a_reason_not_an_error(self, capsys):
        result = self.run(capsys, "--incident", "incidents-99")
        assert result["cleared"] == [] and result["kept"] == [{"id": "incidents-99", "reason": "no local state"}]

    def test_a_targeted_run_leaves_other_incidents_unused_bind_tokens(self, world, capsys):
        named = world.incident(7)
        world.merge_audit(named)
        world.issues[7] = "CLOSED"
        stale = world.state / "binds" / ("f" * 32)
        stale.write_text("incidents-50\n", encoding="utf-8")
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))
        assert self.run(capsys, "--apply", "--incident", named)["stale_tokens"] == 0
        assert stale.is_file()


class TestAtIncidentStart:
    def test_open_clears_old_merged_incidents_and_still_prints_json(self, world, capsys, monkeypatch):
        old = world.incident(7)
        world.merge_audit(old)
        past = time.time() - 20 * 86400
        os.utime(audit_hook.spool_path(old).with_suffix(".closed"), (past, past))
        gh = incident.sre_common.run_gh
        monkeypatch.setattr(
            incident.sre_common,
            "run_gh",
            lambda args, stdin=None, timeout=None: (
                (
                    "[]"
                    if args[:2] == ["issue", "list"]
                    else ""
                    if args[:2] == ["label", "create"]
                    else "https://example.com/x/issues/41\n"
                )
                if args[0] != "api"
                else gh(args, stdin, timeout)
            ),
        )
        assert incident.main(["open", "--title", "T", "--severity", "P3"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["id"] == "incidents-41"
        assert "Cleared local state of 1 earlier incident" in captured.err
        assert not audit_hook.spool_path(old).exists()

    def test_a_failing_cleanup_never_stops_an_incident_opening(self, world, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(state_gc, "run", boom)
        root, doc = sre_common.load_site(str(world.site))
        assert state_gc.auto_clean(doc, root) == 0


# --- homelabia#281: a committed incident stays committed after gc ------------------------------------------------


def cleared(world: World, number: int = 7) -> str:
    """An incident committed, merged, closed and cleared by gc, as on macro after a normal close-out."""
    incident_id = world.incident(number)
    world.merge_audit(incident_id)
    world.issues[number] = "CLOSED"
    assert run_gc(world)["cleared"] == [incident_id]
    return incident_id


def bind_session(world: World, incident_id: str, session: str = "late") -> None:
    """A session still pointing at the incident: a gateway conversation, or one bound again by hand."""
    (world.state / "sessions").mkdir(parents=True, exist_ok=True)
    (world.state / "sessions" / f"{session}.json").write_text(json.dumps({"incident": incident_id}), encoding="utf-8")


def respool(incident_id: str, count: int = 2) -> None:
    """What happened before the fix: a fresh chain started after gc, beside a resolved state file."""
    audit_hook.retired_path(incident_id).unlink(missing_ok=True)
    for index in range(count):
        audit_hook.append(
            incident_id,
            {
                "v": 1,
                "ts": f"2026-11-21T00:00:0{index}Z",
                "event": "approval",
                "via": "chat",
                "command": f"late {index}",
            },
        )
    sre_common.write_incident_state(
        {
            "id": incident_id,
            "repo": "example-org/incidents",
            "number": 7,
            "url": "u",
            "title": "T",
            "status": "resolved",
        },
        make_current=False,
    )


class FakeTracker:  # pylint: disable=too-few-public-methods
    """gh for attach and the timeline: one resolved issue and its comments."""

    def __init__(self, world: World):
        self.world = world
        self.comments: list[dict] = []

    def __call__(self, args, stdin=None, timeout=None):  # noqa: ARG002  pylint: disable=unused-argument
        if args[0] == "api":
            return "\n".join(json.dumps(comment) for comment in self.comments)
        if args[:2] == ["issue", "comment"]:
            self.comments.append(
                {"body": stdin, "created_at": "2026-11-22T00:00:00Z", "author": "agent", "association": "OWNER"}
            )
            return "https://example.com/comment/1\n"
        if args[:2] == ["issue", "view"] and "--jq" in args:
            return self.world.issues.get(int(args[2]), "OPEN") + "\n"
        if args[:2] == ["issue", "view"]:
            body = (
                f"{incident.MARKER}\n**Severity:** P3 (final)\n**Phase:** Resolved\n"
                "**Resolved:** x (2026-11-20T00:00:00Z)\n"
            )
            return json.dumps(
                {
                    "title": "T",
                    "url": f"https://example.com/i/{args[2]}",
                    "body": body,
                    "createdAt": "2026-11-19T00:00:00Z",
                    "labels": [],
                }
            )
        raise AssertionError(f"unexpected gh call: {args}")


class TestRetired:
    def test_gc_leaves_a_tombstone_naming_the_committed_log(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "CLOSED"
        count, head = audit_hook.head(incident_id)
        run_gc(world)
        tombstone = json.loads(audit_hook.retired_path(incident_id).read_text(encoding="utf-8"))
        assert (tombstone["records"], tombstone["head"], tombstone["by"]) == (count, head, "gc")
        assert audit_hook.is_closed(incident_id)

    def test_a_dry_run_retires_nothing(self, world):
        incident_id = world.incident(7)
        world.merge_audit(incident_id)
        world.issues[7] = "CLOSED"
        run_gc(world, apply=False)
        assert not audit_hook.retired_path(incident_id).exists()

    def test_a_session_still_bound_after_gc_records_nothing(self, world):
        incident_id = cleared(world)
        bind_session(world, incident_id)
        audit_hook.handle(
            {"session_id": "late", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}
        )
        assert not audit_hook.spool_path(incident_id).exists(), "catalog-66: the hook re-spooled after gc"

    def test_a_chat_approval_after_gc_is_refused_not_spooled(self, world):
        incident_id = cleared(world)
        decision = incident.state_gc.audit.ChatDecision(
            "approved", "Bash", {"command": "gh issue create"}, "t1", "U1", None
        )
        with pytest.raises(sre_common.SiteError, match="attach incidents-7 --reopen"):
            incident.state_gc.audit.record_chat_decision(incident_id, decision)
        assert not audit_hook.spool_path(incident_id).exists()

    def test_a_stray_spool_beside_a_tombstone_points_at_discard_not_commit(self, world):
        incident_id = cleared(world)
        respool(incident_id)
        state_gc.retire(incident_id, b"", "attach")
        reason = run_gc(world)["kept"][0]["reason"]
        assert "--discard-spool" in reason and "only copy" not in reason

    def test_the_retired_incident_is_not_assessed_again(self, world):
        cleared(world)
        result = run_gc(world)
        assert result["cleared"] == [] and result["kept"] == []

    def test_a_retired_incident_does_not_hold_the_active_marker(self, world):
        cleared(world)
        (world.state / "active").write_text("x\n", encoding="utf-8")
        audit_hook.release_active_marker()
        assert not (world.state / "active").exists()


class TestAttachAfterGc:
    @pytest.fixture(autouse=True)
    def tracker(self, world, monkeypatch) -> FakeTracker:
        fake = FakeTracker(world)
        monkeypatch.setattr(sre_common, "run_gh", fake)
        return fake

    @staticmethod
    def attached(capsys, *argv: str) -> dict:
        assert incident.main(["attach", *argv]) == 0
        return json.loads(capsys.readouterr().out.splitlines()[0])

    def test_plain_attach_to_a_retired_incident_arms_nothing(self, world, capsys):
        incident_id = cleared(world)
        assert self.attached(capsys, incident_id)["bind"] is None
        assert not any((world.state / "binds").glob("*")) if (world.state / "binds").is_dir() else True

    def test_attach_backfills_the_tombstone_for_an_incident_gc_cleared_before_the_fix(self, world, capsys):
        incident_id = cleared(world)
        audit_hook.retired_path(incident_id).unlink()
        assert self.attached(capsys, incident_id)["bind"] is None, "catalog-28: re-attach armed capture"
        assert json.loads(audit_hook.retired_path(incident_id).read_text())["by"] == "attach"

    @pytest.mark.usefixtures("world")
    def test_attach_with_no_merged_log_still_binds(self, capsys):
        assert len(self.attached(capsys, "incidents-12")["bind"]) == 32
        assert not audit_hook.retired_path("incidents-12").exists()

    def test_reopen_after_gc_restores_the_committed_log_and_continues_its_chain(self, world, tracker, capsys):
        incident_id = cleared(world)
        merged = (world.site / "audit" / f"{incident_id}.jsonl").read_bytes()
        assert len(self.attached(capsys, incident_id, "--reopen")["bind"]) == 32
        assert not audit_hook.is_closed(incident_id)
        spool = audit_hook.spool_path(incident_id).read_bytes()
        assert spool.startswith(merged), "a later commit only appends"
        entries = incident.state_gc.audit.read_log(incident_id)
        assert incident.state_gc.audit.verify(entries)["chain_intact"]
        assert entries[-1][1]["event"] == "reopened" and entries[-1][1]["continues"]["records"] == 3
        assert "Reopened" in tracker.comments[-1]["body"]

    def test_reopen_before_gc_lifts_the_closed_marker_so_capture_resumes(self, world, capsys):
        incident_id = world.incident(7)
        self.attached(capsys, incident_id, "--reopen")
        assert not audit_hook.is_closed(incident_id)
        assert incident.state_gc.audit.read_log(incident_id)[-1][1]["event"] == "reopened"

    def test_reopen_refuses_to_build_on_a_stray_spool(self, world, capsys):
        incident_id = cleared(world)
        respool(incident_id)
        state_gc.retire(incident_id, b"", "attach")
        assert incident.main(["attach", incident_id, "--reopen"]) == 2
        assert "--discard-spool" in capsys.readouterr().err
        assert audit_hook.is_closed(incident_id)

    def test_reopen_refuses_when_the_committed_log_cannot_be_read(self, world, capsys):
        incident_id = cleared(world)
        subprocess.run(["git", "-C", str(world.site), "remote", "remove", "origin"], check=True)
        subprocess.run(["git", "-C", str(world.site), "update-ref", "-d", "refs/remotes/origin/main"], check=False)
        assert incident.main(["attach", incident_id, "--reopen"]) == 2
        assert "not reopening" in capsys.readouterr().err
        assert audit_hook.is_closed(incident_id) and not audit_hook.spool_path(incident_id).exists()


class TestDiscardSpool:
    @pytest.fixture(autouse=True)
    def tracker(self, world, monkeypatch) -> FakeTracker:
        fake = FakeTracker(world)
        monkeypatch.setattr(sre_common, "run_gh", fake)
        return fake

    @staticmethod
    def discard(capsys, *argv: str) -> tuple[int, dict, str]:
        code = incident.main(["gc", "--no-fetch", "--discard-spool", *argv])
        captured = capsys.readouterr()
        return code, (json.loads(captured.out) if captured.out.strip() else {}), captured.err

    def test_dry_run_lists_every_record_it_would_drop_and_deletes_nothing(self, world, capsys):
        incident_id = cleared(world)
        respool(incident_id)
        code, result, err = self.discard(capsys, "--incident", incident_id)
        assert code == 0 and result["dry_run"] is True and result["discarded"] is False
        assert [item["command"] for item in result["drop"]] == ["late 0", "late 1"]
        assert result["merged"]["records"] == 3 and "--apply" in err
        assert audit_hook.spool_path(incident_id).is_file()

    def test_apply_drops_the_spool_and_retires_the_incident(self, world, capsys):
        incident_id = cleared(world)
        respool(incident_id)
        code, result, _err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 0 and result["discarded"] is True
        assert not audit_hook.spool_path(incident_id).exists()
        assert json.loads(audit_hook.retired_path(incident_id).read_text())["records"] == 3
        assert (world.site / "audit" / f"{incident_id}.jsonl").is_file(), "the merged log is never touched"

    def test_refused_from_chat(self, world, capsys, monkeypatch):
        incident_id = cleared(world)
        respool(incident_id)
        monkeypatch.setenv(sre_common.NO_CURRENT_ENV, "1")
        code, _result, err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 2 and "not allowed from chat" in err
        assert audit_hook.spool_path(incident_id).is_file()

    @pytest.mark.usefixtures("world")
    def test_needs_exactly_one_incident(self, capsys):
        code, _result, err = self.discard(capsys, "--apply")
        assert code == 2 and "exactly one --incident" in err

    def test_refused_when_the_log_never_merged(self, world, capsys):
        incident_id = world.incident(7)
        code, result, _err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 1 and "not on the audit repository's default branch" in result["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_refused_for_a_live_incident(self, world, capsys):
        incident_id = world.incident(7, resolved=False)
        world.merge_audit(incident_id)
        code, result, _err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 1 and "not resolved" in result["reason"]

    def test_refused_when_the_merged_log_does_not_match_its_pins(self, world, tracker, capsys):
        incident_id = cleared(world)
        respool(incident_id)
        tracker.comments.append(
            {
                "body": timeline.render(
                    "action", "x", sre_common.parse_utc("2026-01-02T03:04:09Z"), "Etc/UTC", (3, "f" * 64)
                ),
                "created_at": "2026-01-02T03:04:09Z",
                "author": "agent",
                "association": "OWNER",
            }
        )
        code, result, _err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 1 and "pinned hashes" in result["reason"]
        assert audit_hook.spool_path(incident_id).is_file()

    def test_passes_when_the_merged_log_matches_its_pins(self, world, tracker, capsys):
        incident_id = cleared(world)
        merged = (world.site / "audit" / f"{incident_id}.jsonl").read_text(encoding="utf-8").splitlines()
        head = hashlib.sha256(merged[-1].encode()).hexdigest()
        respool(incident_id)
        tracker.comments.append(
            {
                "body": timeline.render(
                    "action", "x", sre_common.parse_utc("2026-01-02T03:04:09Z"), "Etc/UTC", (3, head)
                ),
                "created_at": "2026-01-02T03:04:09Z",
                "author": "agent",
                "association": "OWNER",
            }
        )
        code, result, _err = self.discard(capsys, "--incident", incident_id, "--apply")
        assert code == 0 and result["discarded"] is True
