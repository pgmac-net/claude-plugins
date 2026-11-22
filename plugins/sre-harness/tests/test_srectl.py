"""srectl: the person-facing front end over the harness scripts, and its shell completion."""

from __future__ import annotations

import os
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


BASH_WORDBREAKS = " \t\n\"'@><=;|&(:"


def bash_complete(tmp_path: Path, line: str, extra_env: dict[str, str] | None = None) -> list[str]:
    """Run the generated bash function the way bash calls it: COMP_LINE, COMP_POINT and the usual word breaks."""
    link = tmp_path / "bin" / "srectl"
    link.parent.mkdir(exist_ok=True)
    if not link.exists():
        link.symlink_to(SCRIPTS / "srectl.py")
    script = tmp_path / "c.bash"
    script.write_text(srectl.BASH, encoding="utf-8")
    probe = (
        f"source {script}; COMP_WORDBREAKS=$TEST_BREAKS; COMP_LINE=$TEST_LINE; COMP_POINT=${{#COMP_LINE}}; "
        f'COMP_WORDS=(); COMP_CWORD=0; _srectl_complete {link}; printf "%s\\n" "${{COMPREPLY[@]}}"'
    )
    env = {**os.environ, **(extra_env or {}), "TEST_LINE": line, "TEST_BREAKS": BASH_WORDBREAKS}
    done = subprocess.run(["bash", "--norc", "-c", probe], capture_output=True, text=True, check=False, env=env)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_bash_completion_end_to_end(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    assert bash_complete(tmp_path, "srectl audit record --kind ") == [
        "approval",
        "denial",
        "decision",
        "command",
        "note",
    ]
    assert bash_complete(tmp_path, "srectl audit record --kind d") == ["denial", "decision"]
    assert bash_complete(tmp_path, "sre slo st") == ["status"]


def test_bash_completion_of_names_survives_its_word_breaks(tmp_path, named_env):
    """Bash splits `component:cl` and `--incident=inc` at ':' and '='; candidates must not repeat the left part."""
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed")
    state = tmp_path / "state" / "sre-harness" / "incidents"
    state.mkdir(parents=True)
    (state / "incidents-97.json").write_text("{}", encoding="utf-8")
    env = {**named_env, "XDG_STATE_HOME": str(tmp_path / "state")}
    assert bash_complete(tmp_path, "srectl autonomy match --service component:d", env) == ["deployment", "dns"]
    assert bash_complete(tmp_path, "srectl audit show --incident=incidents-9", env) == ["incidents-97"]
    assert bash_complete(tmp_path, "srectl incident open --services ingress,d", env) == [
        "ingress,deployment",
        "ingress,dns",
    ]


def test_unknown_completion_arguments_are_refused(capsys):
    assert srectl.main(["completion", "powershell"]) == 2
    assert "usage: srectl completion" in capsys.readouterr().err


# --- service and SLO names from the site -----------------------------------------------------------------

COMPONENT = """\
# a comment before the document
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: {name}
  title: "{name} title"
spec:
  type: service
  owner: user:someone
  dependsOn:
    - component:other
"""
SLO_FILE = """\
apiVersion: openslo/v1
kind: SLO
metadata:
  name: {name}
spec:
  service: {service}
"""


def write_site(root: Path, services: list[str], slos: dict[str, list[str]], catalog: str = "catalog") -> Path:
    """A minimal site: a catalog with Components (and kinds that are not), and SLO files in service directories."""
    (root / catalog / "services").mkdir(parents=True)
    (root / "site.yaml").write_text(
        "schema_version: 7\n"
        "catalog:\n"
        f"  path: {catalog}   # where entities live\n"
        "  sources:\n"
        "    - type: ansible\n"
        "      path: elsewhere\n"
        "slo:\n"
        f'  journeys_path: "{catalog}/journeys"\n',
        encoding="utf-8",
    )
    for name in services:
        (root / catalog / "services" / f"{name}.yaml").write_text(COMPONENT.format(name=name), encoding="utf-8")
    for kind, name in (("System", "edge"), ("Resource", "box")):
        (root / catalog / "services" / f"{name}.yaml").write_text(
            COMPONENT.format(name=name).replace("kind: Component", f"kind: {kind}"), encoding="utf-8"
        )
    for service, names in slos.items():
        (root / catalog / "journeys" / service).mkdir(parents=True)
        for name in names:
            path = root / catalog / "journeys" / service / f"{name}.slo.yaml"
            path.write_text(SLO_FILE.format(name=name, service=service), encoding="utf-8")
    return root


@pytest.fixture
def named_site(tmp_path) -> Path:
    return write_site(
        tmp_path / "named-site",
        ["ingress", "dns", "deployment"],
        {"ingress": ["ingress-requests", "ingress-web"], "dns": ["dns-resolve"]},
    )


@pytest.fixture
def named_env(tmp_path, named_site) -> dict[str, str]:
    return {"SRE_HARNESS_SITE": str(named_site), "XDG_CONFIG_HOME": str(tmp_path / "nocfg")}


def test_service_names_are_the_catalog_components_only(named_env):
    found = values(srectl.complete(["slo", "status", "--service", ""], env=named_env))
    assert found == ["deployment", "dns", "ingress"]
    assert values(srectl.complete(["budget", "nagios", "--service", "d"], env=named_env)) == ["deployment", "dns"]


def test_a_service_name_carries_component_where_the_command_requires_it(named_env):
    found = values(srectl.complete(["autonomy", "match", "--service", ""], env=named_env))
    assert found == ["component:deployment", "component:dns", "component:ingress"]
    found = values(srectl.complete(["autonomy", "run", "--service", "component:i"], env=named_env))
    assert found == ["component:ingress"]


def test_typing_component_switches_a_bare_command_to_prefixed_names(named_env):
    found = values(srectl.complete(["slo", "status", "--service", "component:d"], env=named_env))
    assert found == ["component:deployment", "component:dns"]


def test_slo_names_narrow_to_the_service_already_given(named_env):
    both = srectl.complete(["slo", "status", "--service", "ingress", "--slo", ""], env=named_env)
    assert both == [("ingress-requests", "ingress"), ("ingress-web", "ingress")]
    prefixed = srectl.complete(["slo", "status", "--service", "component:dns", "--slo", ""], env=named_env)
    assert values(prefixed) == ["dns-resolve"]
    assert values(srectl.complete(["slo", "status", "--service=dns", "--slo", ""], env=named_env)) == ["dns-resolve"]


def test_every_slo_is_offered_without_a_service_and_shows_its_service(named_env):
    assert srectl.complete(["slo", "status", "--slo", ""], env=named_env) == [
        ("dns-resolve", "dns"),
        ("ingress-requests", "ingress"),
        ("ingress-web", "ingress"),
    ]
    assert values(srectl.complete(["budget", "nagios", "--slo", "ingress-w"], env=named_env)) == ["ingress-web"]


def test_a_services_list_completes_its_last_element_and_skips_what_is_listed(named_env):
    found = values(srectl.complete(["incident", "open", "--services", "ingress,d"], env=named_env))
    assert found == ["ingress,deployment", "ingress,dns"]
    found = values(srectl.complete(["incident", "set", "--services", "dns,ingress,"], env=named_env))
    assert found == ["dns,ingress,deployment"]
    found = values(srectl.complete(["incident", "set", "--services", "component:dns,"], env=named_env))
    assert found == ["component:dns,deployment", "component:dns,ingress"]


def test_the_equals_form_completes_names_too(named_env):
    found = values(srectl.complete(["slo", "status", "--service=d"], env=named_env))
    assert found == ["--service=deployment", "--service=dns"]
    found = values(srectl.complete(["incident", "open", "--services=ingress,d"], env=named_env))
    assert found == ["--services=ingress,deployment", "--services=ingress,dns"]


def test_a_site_on_the_line_wins_over_the_environment(tmp_path, named_env):
    other = write_site(tmp_path / "other-site", ["zeta"], {"zeta": ["zeta-slo"]})
    line = ["slo", "status", "--site", str(other)]
    assert values(srectl.complete([*line, "--service", ""], env=named_env)) == ["zeta"]
    assert values(srectl.complete([*line, "--slo", ""], env=named_env)) == ["zeta-slo"]
    assert values(srectl.complete(["slo", "status", f"--site={other}", "--service", ""], env=named_env)) == ["zeta"]


def test_the_pointer_file_is_used_when_nothing_else_names_a_site(tmp_path, named_site):
    config = tmp_path / "config" / "sre-harness"
    config.mkdir(parents=True)
    (config / "site").write_text(f"{named_site}\n", encoding="utf-8")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert values(srectl.complete(["slo", "status", "--service", "i"], env=env)) == ["ingress"]


def test_no_site_means_no_names_and_no_error(tmp_path):
    nothing = {"XDG_CONFIG_HOME": str(tmp_path / "nocfg")}
    assert srectl.complete(["slo", "status", "--service", ""], env=nothing) == []
    assert srectl.complete(["slo", "status", "--slo", ""], env={**nothing, "SRE_HARNESS_SITE": "/does/not/exist"}) == []


def test_a_site_with_no_readable_catalog_means_no_names(tmp_path):
    empty = tmp_path / "empty-site"
    empty.mkdir()
    (empty / "site.yaml").write_text("", encoding="utf-8")
    env = {"SRE_HARNESS_SITE": str(empty), "XDG_CONFIG_HOME": str(tmp_path / "nocfg")}
    assert srectl.complete(["slo", "status", "--service", ""], env=env) == []
    assert srectl.complete(["slo", "status", "--slo", ""], env=env) == []


def test_a_catalog_path_that_leaves_the_site_is_not_read(tmp_path):
    outside = write_site(tmp_path / "outside", ["secret-name"], {})
    site = tmp_path / "inner"
    site.mkdir()
    (site / "site.yaml").write_text(f"catalog:\n  path: {outside / 'catalog'}\n", encoding="utf-8")
    assert srectl.site_names(site.resolve()) == ([], [])
    (site / "site.yaml").write_text("catalog:\n  path: ../outside/catalog\n", encoding="utf-8")
    assert srectl.site_names(site.resolve()) == ([], [])


def test_a_linked_entity_file_is_not_followed(tmp_path, named_site):
    real = tmp_path / "elsewhere.yaml"
    real.write_text(COMPONENT.format(name="linked"), encoding="utf-8")
    (named_site / "catalog" / "services" / "linked.yaml").symlink_to(real)
    services, _slos = srectl.site_names(named_site.resolve())
    assert "linked" not in services


def test_the_scanner_takes_only_a_direct_child_of_a_top_level_key():
    lines = ["catalog:", "  sources:", "    - path: deeper", "  path: top   # note", "other:", "  path: no"]
    assert srectl.yaml_child(lines, "catalog", "path") == "top"
    assert srectl.yaml_child(lines, "other", "path") == "no"
    assert srectl.yaml_child(lines, "missing", "path") is None
    assert srectl.yaml_child(["slo:", '  journeys_path: "a b/c"'], "slo", "journeys_path") == "a b/c"
    assert srectl.yaml_child(["slo:", "  journeys_path: 'x'"], "slo", "journeys_path") == "x"


def test_the_scanner_reads_the_first_document_only():
    lines = [
        "# lead",
        "kind: Component",
        "metadata:",
        "  name: one",
        "---",
        "kind: Resource",
        "metadata:",
        "  name: two",
    ]
    assert srectl.yaml_top(lines, "kind") == "Component"
    assert srectl.yaml_child(lines, "metadata", "name") == "one"
    assert srectl.yaml_top(["---", "kind: System"], "kind") == "System"


def test_names_in_the_shipped_template_site_equal_the_real_loaders(site):
    """The scanner is a second, tiny parser: hold it to the real catalog and SLO loaders on a valid site."""
    import slo  # noqa: PLC0415  pylint: disable=import-outside-toplevel
    import sre_common  # noqa: PLC0415  pylint: disable=import-outside-toplevel
    import stpa  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    root, doc = sre_common.load_site(str(site))
    components = sorted(ref.split(":", 1)[1] for ref in stpa.catalog_services(root, doc))
    found = slo.load(root, doc)
    assert components, "the template should have a service to compare"
    assert srectl.site_names(root) == (components, sorted((one.name, one.service) for one in found.slos))
