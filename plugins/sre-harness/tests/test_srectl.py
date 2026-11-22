"""srectl: the person-facing front end over the harness scripts, and its shell completion."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import srectl

SCRIPTS = Path(srectl.__file__).resolve().parent
# Scripts that are plumbing, not commands a person runs: srectl does not list them.
HIDDEN = {"audit_hook.py", "kube_read.py", "check_identifiers.py"}


@pytest.fixture
def exec_calls(monkeypatch):
    """Stand in for os.execv so dispatch can be inspected instead of replaced."""
    calls: list[list[str]] = []

    def fake(_path: str, argv: list[str]) -> None:
        calls.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(srectl.os, "execv", fake)
    monkeypatch.setattr(srectl.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def values(result) -> list[str]:
    assert result != srectl.FILES
    return [value for value, _help in result]


# --- the registry ----------------------------------------------------------------------------------------


def test_every_registered_script_exists():
    for _words, script, _section in srectl.REGISTRY:
        assert (SCRIPTS / script).is_file(), script


def test_every_command_line_script_is_registered_or_deliberately_hidden():
    runnable = {p.name for p in SCRIPTS.glob("*.py") if '__name__ == "__main__"' in p.read_text(encoding="utf-8")}
    registered = {script for _words, script, _section in srectl.REGISTRY}
    assert runnable - HIDDEN - {"srectl.py"} == registered, (
        "a script was added or removed: register it in srectl.REGISTRY, or add it to HIDDEN here on purpose"
    )


def test_a_group_name_is_never_also_a_verb_of_another_group():
    assert srectl.verbs("catalog") == ["check", "sync"]
    assert srectl.verbs("site") == ["check"]
    assert srectl.verbs("incident") == []


# --- the spec --------------------------------------------------------------------------------------------


def test_the_committed_spec_is_current():
    committed = srectl.SPEC_PATH.read_text(encoding="utf-8")
    assert srectl.render_spec(srectl.build_spec()) == committed, (
        "a script's options changed: run `srectl completion --regenerate` and commit srectl_spec.json"
    )


def test_the_spec_knows_options_and_subcommands():
    entries = srectl.build_spec()["entries"]
    record = entries["audit"]["commands"]["record"]
    kind = next(o for o in record["options"] if "--kind" in o["flags"])
    assert kind["value"] is True
    assert "approval" in kind["choices"]
    verify = entries["audit"]["commands"]["verify"]
    check_anchors = next(o for o in verify["options"] if "--check-anchors" in o["flags"])
    assert check_anchors["value"] is False
    assert entries["audit"]["required"] is True


def test_a_script_that_builds_its_parser_in_main_is_captured():
    entries = srectl.build_spec()["entries"]
    assert any("--site" in o["flags"] for o in entries["catalog check"]["options"])


# --- completion ------------------------------------------------------------------------------------------


def test_groups_complete_by_prefix_and_include_completion():
    assert values(srectl.complete([""]))[-1] == "completion"
    assert values(srectl.complete(["s"])) == ["slo", "stpa", "site"]


def test_a_script_group_completes_its_subcommands():
    assert values(srectl.complete(["slo", ""])) == ["check", "generate", "dashboard", "status"]
    assert values(srectl.complete(["incident", "s"])) == ["status", "set"]


def test_a_multi_script_group_completes_its_verbs():
    assert values(srectl.complete(["catalog", ""])) == ["check", "sync"]
    assert values(srectl.complete(["catalog", "sync", "--"])) != []


def test_flags_complete_for_the_subcommand_and_its_group():
    found = values(srectl.complete(["audit", "record", "--"]))
    assert "--kind" in found
    assert "--site" in found  # a group-level option stays available after the subcommand
    assert "--no-pr" not in found  # belongs to `commit`


def test_choices_complete_after_their_flag():
    assert values(srectl.complete(["audit", "record", "--kind", ""])) == [
        "approval",
        "denial",
        "decision",
        "command",
        "note",
    ]
    assert values(srectl.complete(["audit", "record", "--kind", "a"])) == ["approval"]


def test_incident_ids_come_from_local_state(tmp_path):
    state = tmp_path / "sre-harness" / "incidents"
    state.mkdir(parents=True)
    for name in ("incidents-97", "incidents-102"):
        (state / f"{name}.json").write_text("{}", encoding="utf-8")
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert values(srectl.complete(["timeline", "list", "--incident", ""], env=env)) == ["incidents-102", "incidents-97"]
    assert values(srectl.complete(["timeline", "list", "--incident", "incidents-9"], env=env)) == ["incidents-97"]


def test_no_incident_state_means_no_candidates(tmp_path):
    assert srectl.incident_ids({"XDG_STATE_HOME": str(tmp_path)}) == []


def test_paths_are_left_to_the_shell():
    assert srectl.complete(["incident", "status", "--site", ""]) == srectl.FILES
    assert srectl.complete(["audit", "show", "--site", "/tm"]) == srectl.FILES


def test_the_equals_form_keeps_its_flag(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert srectl.complete(["audit", "record", "--kind=n"], env=env) == [("--kind=note", "")]


def test_bash_splits_the_equals_form_into_three_words(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert srectl.normalise_words(["audit", "record", "--kind", "=", "n"]) == ["audit", "record", "--kind", "n"]
    assert srectl.normalise_words(["audit", "record", "--kind", "="]) == ["audit", "record", "--kind", ""]
    assert values(srectl.complete(["audit", "record", "--kind", "=", "n"], env=env)) == ["note"]


def test_an_unknown_group_completes_nothing():
    assert srectl.complete(["nope", ""]) == []


def test_completion_subcommand_lists_shells_and_flags():
    assert values(srectl.complete(["completion", ""])) == ["bash", "zsh", "fish"]
    assert values(srectl.complete(["completion", "--r"])) == ["--regenerate"]


def test_complete_prints_value_tab_help(capsys):
    assert srectl.main(["__complete", "catalog", "c"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("check\t")


# --- dispatch --------------------------------------------------------------------------------------------


def test_a_group_runs_its_script_with_the_remaining_words(exec_calls):
    with pytest.raises(SystemExit):
        srectl.main(["incident", "status", "--incident", "incidents-97"])
    assert exec_calls == [
        ["/usr/bin/uv", "run", "--script", str(SCRIPTS / "incident.py"), "status", "--incident", "incidents-97"]
    ]


def test_a_verb_picks_the_script(exec_calls):
    with pytest.raises(SystemExit):
        srectl.main(["catalog", "sync", "--check"])
    assert exec_calls[0][3:] == [str(SCRIPTS / "catalog_sync.py"), "--check"]
    with pytest.raises(SystemExit):
        srectl.main(["site", "check", "--json"])
    assert exec_calls[1][3:] == [str(SCRIPTS / "validate_site.py"), "--json"]


def test_a_single_script_group_passes_everything_through(exec_calls):
    with pytest.raises(SystemExit):
        srectl.main(["report", "--month", "2026-10", "-h"])
    assert exec_calls[0][3:] == [str(SCRIPTS / "report.py"), "--month", "2026-10", "-h"]


def test_help_flags_reach_the_script(exec_calls):
    with pytest.raises(SystemExit):
        srectl.main(["slo", "check", "-h"])
    assert exec_calls[0][3:] == [str(SCRIPTS / "slo.py"), "check", "-h"]


def test_a_group_with_a_required_subcommand_lists_its_commands(exec_calls, capsys):
    assert srectl.main(["incident"]) == 2
    assert not exec_calls
    err = capsys.readouterr().err
    assert "usage: srectl incident <command>" in err
    assert "status" in err


def test_a_multi_script_group_alone_lists_its_verbs(exec_calls, capsys):
    assert srectl.main(["catalog"]) == 2
    assert "sync" in capsys.readouterr().out
    assert srectl.main(["catalog", "-h"]) == 0
    assert not exec_calls


def test_an_unknown_group_suggests_the_nearest(exec_calls, capsys):
    with pytest.raises(SystemExit) as stopped:
        srectl.main(["inciden"])
    assert stopped.value.code == 2
    assert "did you mean 'incident'?" in capsys.readouterr().err
    assert not exec_calls


def test_an_unknown_verb_is_refused_before_running_anything(exec_calls, capsys):
    with pytest.raises(SystemExit) as stopped:
        srectl.main(["catalog", "snyc"])
    assert stopped.value.code == 2
    assert "did you mean 'sync'?" in capsys.readouterr().err
    assert not exec_calls


def test_missing_uv_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(srectl.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as stopped:
        srectl.main(["slo", "status"])
    assert stopped.value.code == 127
    assert "uv is not installed" in capsys.readouterr().err


def test_bare_srectl_lists_every_group(capsys):
    assert srectl.main([]) == 0
    out = capsys.readouterr().out
    for group in srectl.groups():
        assert f"  {group} " in out
    assert "completion" in out


def test_scripts_called_directly_stay_the_way_agents_run_them():
    """srectl adds nothing the scripts depend on: each still runs on its own."""
    done = subprocess.run(
        [str(SCRIPTS / "srectl.py"), "__complete", "slo", ""], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0
    assert done.stdout.startswith("check\t")


# --- shell scripts ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("shell", srectl.SHELLS)
def test_each_shell_gets_a_script_for_both_names(shell, capsys):
    assert srectl.main(["completion", shell]) == 0
    text = capsys.readouterr().out
    assert "__complete" in text
    assert "srectl" in text
    assert "sre" in text


@pytest.mark.parametrize("shell", srectl.SHELLS)
def test_each_script_parses_in_its_shell(shell, tmp_path):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    script = tmp_path / f"srectl.{shell}"
    script.write_text(srectl.SHELL_SCRIPTS[shell], encoding="utf-8")
    done = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_bash_completion_end_to_end(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    link = tmp_path / "bin" / "srectl"
    link.parent.mkdir()
    link.symlink_to(SCRIPTS / "srectl.py")
    script = tmp_path / "c.bash"
    script.write_text(srectl.BASH, encoding="utf-8")
    probe = (
        f'source {script}; COMP_WORDS=(srectl audit record --kind ""); COMP_CWORD=4; '
        f'_srectl_complete {link}; echo "${{COMPREPLY[*]}}"'
    )
    done = subprocess.run(["bash", "--norc", "-c", probe], capture_output=True, text=True, check=False)
    assert done.stdout.split() == ["approval", "denial", "decision", "command", "note"], done.stderr


def test_unknown_completion_arguments_are_refused(capsys):
    assert srectl.main(["completion", "powershell"]) == 2
    assert "usage: srectl completion" in capsys.readouterr().err
