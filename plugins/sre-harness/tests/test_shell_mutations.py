"""Mutation detection per simple command, and approval pairing (claude-plugins#32)."""

from __future__ import annotations

import json

import audit
import pytest

SCRIPTS = audit.SCRIPTS_DIR
PATCH = 'kubectl --context main -n shop patch deployment app-x --type merge -p \'{"spec":{"replicas":2}}\''
PATCH_AS_APPROVED = 'kubectl --context main -n shop patch deployment app-x --type merge -p {"spec":{"replicas":2}}'
DELETE = "kubectl --context main -n shop delete pod app-x-0"


class TestClassification:
    @pytest.mark.parametrize(
        "command",
        [
            "kubectl get pods -n shop\nkubectl delete pod app-x-0 -n shop",
            "ssh host-a 'mount -t nfs | while read m; do r=$(timeout 6 sudo -n stat \"$m\"); done; sudo umount /mnt/x'",
            "ssh -o ConnectTimeout=8 host-a 'sudo -n systemctl restart app-x'",
            "kubectl exec app-x-0 -n shop -- sh -c 'rm -rf /cache/*'",
            "docker exec app-x rm -f /data/lock",
            "sudo timeout 30 nohup reboot",
            "bash <<EOF\nkubectl delete ns shop\nEOF",
            "ssh host-a <<'EOF'\nsudo systemctl stop app-x\nEOF",
            "cat > /tmp/notes.md <<EOF\n$(kubectl delete pod app-x-0)\nEOF",
            'echo "$(kubectl delete pod app-x-0)"',
            "echo `kubectl delete pod app-x-0`",
            "diff <(kubectl get cm a) <(kubectl delete cm b)",
            "find /var/tmp -name '*.lock' -exec rm {} \\;",
            "ls | xargs -I{} rm {}",
            "watch -n 5 'kubectl delete pod app-x-0'",
            "env FOO=1 sudo -u app systemctl try-restart app-x",
            "dd if=/dev/zero of=/dev/sdz bs=1M count=1",
            "mount -t nfs -o ro host-a:/export /mnt/x",
            "mount -a",
            "microk8s kubectl delete pod app-x-0",
            "uv pip install requests",
            "python3 -c 'import os' && rm -rf /var/lib/app",
            "VAR=1 rm -rf /var/lib/app",
            "if true; then kubectl delete pod app-x-0; fi",
            "kubectl -n shop rollout restart deployment/app-x && kubectl -n shop rollout status deployment/app-x",
            "git -C /srv/repo push origin main",
            "sed -Ei 's/a/b/' /etc/app.conf",
            "C1='kubectl -n shop delete pod app-x-0'\n$C1",
            "export C1='kubectl -n shop delete pod app-x-0'; ${C1}",
            "$UNKNOWN -rf /var/lib/app",
            "ssh host-a -- sudo reboot",
            "bash -o pipefail -c 'kubectl delete pod app-x-0 | tee /tmp/log'",
            "eval rm -rf /var/lib/app",
            "sudo -s <<EOF\nsystemctl stop app-x\nEOF",
            'x=\'rm -rf /var/lib/app\'\necho "$(kubectl exec app-x-0 -- sh -c "$x")"',
        ],
    )
    def test_mutations_inside_lines_carriers_and_substitutions_are_seen(self, command):
        assert audit.is_mutation(command), command

    @pytest.mark.parametrize(
        "command",
        [
            "git push -u origin docs/pir-app-oom-kill 2>&1 | tail -3".replace("git push -u", "git show --stat"),
            "git worktree add .wt/pir-app-oom-kill -b docs/pir-app-oom-kill origin/main",
            "gh pr create --head docs/pir-app-oom-kill --title 'pir: app OOM kill and stale mounts'",
            "C1='kubectl -n shop patch deployment app-x -p {}'\nC2='kubectl -n shop delete pod app-x-0'",
            "mount -t nfs,nfs4 | awk '{print $1}'",
            "for p in $(ls /proc); do dd if=/proc/$p/fd/0 bs=1 count=0 2>&1 | grep -qi stale; done",
            "docker exec app-x sh -c 'ls /opt && /opt/check --port 30000'",
            "kubectl exec app-x-0 -n shop -- sh -c 'cat /proc/1/status'",
            "uv run scripts/timeline.py add --type action --text 'person restarted NFS on host-a; the mount is back'",
            "grep -n -E 'invoked oom-killer|Killed process' /var/log/kern.log",
            "cat > /tmp/body.md <<'EOF'\nrm -rf / and kubectl delete ns shop are words here\nEOF",
            "git commit -q -F - <<'EOF'\ndocs: app OOM kill\n\nkill, mount and reboot are prose\nEOF",
            "kubectl wait sts/app-x --for=jsonpath='{.spec.replicas}'=1 --timeout=60s",
            "kubectl get application app-x -o custom-columns=N:.metadata.name,SH:.spec.syncPolicy.automated.selfHeal",
            "true # kubectl delete ns shop",
            "echo $((3 + 4))",
            "command -v rm",
            'p=\'ls /proc\'\necho "$(kubectl exec app-x-0 -- sh -c "$p")"',
            'for p in a b; do echo "$p OPEN" || echo "$p refused"; done',
            'awk "{print \\$1\\" \\"\\$3}" /proc/mounts',
        ],
    )
    def test_prose_data_and_read_only_forms_are_not(self, command):
        assert not audit.is_mutation(command), command

    def test_an_argument_a_program_could_run_is_still_judged(self):
        assert audit.is_mutation("uv run scripts/audit.py record --kind note --text 'rm -rf x'")
        assert audit.is_mutation("some-tool --do 'kubectl delete ns shop'")

    def test_nested_text_that_does_not_parse_is_judged_where_a_command_could_start(self):
        assert audit.is_mutation('some-tool --text "it\'s broken; reboot"')
        assert not audit.is_mutation("some-tool --text \"it's the runbook's reboot step\"")

    def test_an_unparsable_line_falls_back_to_its_whole_text(self):
        assert audit.is_mutation("echo 'unterminated; kill")


class TestPairing:
    @staticmethod
    def entries(*records: dict) -> list[tuple[str, dict]]:
        return [(json.dumps(r), {"ts": "2026-01-02T03:04:05Z", **r}) for r in records]

    @staticmethod
    def attempt(command: str, session: str = "s1") -> dict:
        return {
            "event": "attempt",
            "tool": "Bash",
            "capture": "hook",
            "session": session,
            "input": {"command": command},
        }

    @staticmethod
    def complete(command: str, session: str = "s1") -> dict:
        return {
            "event": "complete",
            "tool": "Bash",
            "capture": "hook",
            "session": session,
            "input": {"command": command},
        }

    @staticmethod
    def approval(command: str, **extra) -> dict:
        return {"event": "approval", "capture": "self-reported", "command": command, **extra}

    def found(self, *records: dict) -> list[dict]:
        return audit.unauthorised_mutations(self.entries(*records))

    def test_quoting_does_not_matter_when_the_words_are_the_same(self):
        assert self.found(self.approval(PATCH_AS_APPROVED), self.attempt(PATCH)) == []
        assert self.found(self.approval(PATCH), self.attempt(PATCH)) == []

    def test_a_recorded_argv_pairs_too(self):
        approval = self.approval(
            "anything", argv=["kubectl", "--context", "main", "-n", "shop", "delete", "pod", "app-x-0"]
        )
        assert self.found(approval, self.attempt(DELETE)) == []

    def test_an_extra_flag_is_a_different_command(self):
        found = self.found(self.approval(DELETE), self.attempt(DELETE + " --wait=true --timeout=90s"))
        assert [item["segments"] for item in found] == [[DELETE + " --wait=true --timeout=90s"]]

    def test_each_simple_command_needs_its_own_approval(self):
        line = f"{PATCH}\nkubectl get pods -n shop\n{DELETE}"
        found = self.found(self.approval(PATCH_AS_APPROVED), self.attempt(line))
        assert found[0]["segments"] == [DELETE]
        assert self.found(self.approval(PATCH_AS_APPROVED), self.approval(DELETE), self.attempt(line)) == []

    def test_an_approval_of_the_whole_line_covers_it(self):
        line = f"{PATCH} && {DELETE}"
        assert self.found(self.approval(line), self.attempt(line)) == []

    def test_an_approval_recorded_earlier_in_the_same_call_counts(self):
        line = f"uv run scripts/audit.py record --kind approval --command '{DELETE}' --text ok\n{DELETE}"
        assert self.found(self.attempt(line), self.approval(DELETE), self.complete(line)) == []

    def test_an_approval_held_in_a_variable_counts(self):
        line = f"C1='{DELETE}'\nuv run scripts/audit.py record --kind approval --command \"$C1\" --text ok\n{DELETE}"
        assert self.found(self.attempt(line), self.approval(DELETE), self.complete(line)) == []

    def test_an_approval_recorded_after_the_command_in_the_same_call_does_not(self):
        line = f"{DELETE}\nuv run scripts/audit.py record --kind approval --command '{DELETE}' --text late"
        found = self.found(self.attempt(line), self.approval(DELETE), self.complete(line))
        assert found[0]["segments"] == [DELETE]

    def test_a_same_call_approval_needs_the_call_to_have_completed(self):
        line = f"uv run scripts/audit.py record --kind approval --command '{DELETE}' --text ok\n{DELETE}"
        assert len(self.found(self.attempt(line), self.approval(DELETE))) == 1

    def test_an_approval_with_no_recording_segment_in_the_call_does_not_count_for_it(self):
        assert len(self.found(self.attempt(DELETE), self.approval(DELETE), self.complete(DELETE))) == 1

    def test_an_unused_same_call_approval_stands_for_a_later_call(self):
        line = f"uv run scripts/audit.py record --kind approval --command '{DELETE}' --text ok"
        records = (self.attempt(line), self.approval(DELETE), self.complete(line), self.attempt(DELETE))
        assert self.found(*records) == []
        assert len(self.found(*records, self.attempt(DELETE))) == 1

    def test_a_mutation_in_a_substitution_needs_approval_too(self):
        found = self.found(self.attempt(f'echo "$({DELETE})"'))
        assert found[0]["segments"] == [DELETE]
