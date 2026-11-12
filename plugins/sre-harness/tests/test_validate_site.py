"""Tests for validate_site.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import validate_site as vs
import yaml


def errors_for(site: Path) -> list[str]:
    return vs.validate(site).errors


def test_template_is_valid(site):
    report = vs.validate(site)
    assert report.errors == []
    assert report.summary["site"] == "example"
    assert report.summary["tiers"] == ["1:critical", "2:standard", "3:best-effort"]
    assert report.summary["chat_configured"] is False


def test_unknown_key_is_rejected(edit_site):
    site = edit_site(lambda doc: doc.update({"monitering": []}))
    assert any("monitering" in err for err in errors_for(site))


def test_wrong_schema_version_is_rejected(edit_site):
    site = edit_site(lambda doc: doc.update({"schema_version": 2}))
    assert any("schema_version" in err for err in errors_for(site))


def test_reserved_sections_must_stay_empty(edit_site):
    site = edit_site(lambda doc: doc.update({"chat": {"channel": "ops"}}))
    assert any(err.startswith("site.yaml: chat") for err in errors_for(site))


@pytest.mark.parametrize(
    "value",
    [
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "xoxb-" + "123456789012-abcdefghijkl",
        "https://admin:" + "hunter2@monitoring.example.com/",
        "Zq3vT8mK1xW9pL4rB7nC2yH6dF0sJ5gA" + "Xe8uR3oV",
    ],
)
def test_secret_shaped_values_are_rejected_without_echo(edit_site, value):
    site = edit_site(lambda doc: doc["site"].update({"description": value}))
    found = errors_for(site)
    assert any("looks like a secret" in err for err in found)
    assert all(value not in err for err in found)


TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["monitoring"][0].update({"credentials_env": [TOKEN]}),
        lambda doc: doc.update({"chat": {"token": TOKEN}}),
        lambda doc: doc["monitoring"][0].update({"type": TOKEN}),
        lambda doc: doc["site"].update({"name": TOKEN}),
        lambda doc: doc.update({"schema_version": TOKEN}),
        lambda doc: doc["repos"]["incidents"].update({"fallback_url": TOKEN}),
    ],
)
def test_schema_errors_never_echo_values(edit_site, mutate):
    report = vs.validate(edit_site(mutate))
    assert report.errors
    assert all(TOKEN not in err for err in report.errors)
    assert TOKEN not in str(report.as_dict())


def test_env_var_names_are_not_secrets():
    assert vs.find_secrets({"credentials_env": ["GRAFANA_TOKEN", "NAGIOS_PASSWORD"]}) == []


def test_audit_in_public_repo_is_rejected(edit_site):
    def mutate(doc):
        doc["repos"]["incidents"]["visibility"] = "public"
        doc["audit"]["repo"] = "incidents"

    site = edit_site(mutate)
    assert any("not private" in err for err in errors_for(site))


def test_unknown_repo_alias_is_rejected(edit_site):
    site = edit_site(lambda doc: doc["tracker"].update({"repo": "nowhere"}))
    assert any("'nowhere' is not a key of repos" in err for err in errors_for(site))


def test_duplicate_monitoring_names_are_rejected(edit_site):
    site = edit_site(lambda doc: doc["monitoring"].append(dict(doc["monitoring"][0])))
    assert any("used 2 times" in err for err in errors_for(site))


def test_missing_in_site_paths_are_errors(site):
    (site / "tiers.yaml").unlink()
    (site / "audit" / "README.md").unlink()
    (site / "audit").rmdir()
    found = errors_for(site)
    assert any("tiers_file" in err for err in found)
    assert any("audit/path" in err for err in found)


def test_missing_external_checkout_only_warns(site):
    report = vs.validate(site)
    assert report.ok
    assert any("repos/incidents/path" in warning for warning in report.warnings)


def test_duplicate_tier_ids_are_rejected(site):
    path = site / "tiers.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["tiers"][1]["id"] = 1
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert any("tier id '1' is used 2 times" in err for err in errors_for(site))


def test_invalid_tier_requirement_is_rejected(site):
    path = site / "tiers.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["tiers"][0]["requires"].append("pager")
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert any(err.startswith("tiers.yaml:") for err in errors_for(site))


class TestResolveSite:
    def test_explicit_path_wins(self, site, tmp_path):
        env = {"SRE_HARNESS_SITE": str(tmp_path / "other")}
        assert vs.resolve_site(str(site), env=env, home=tmp_path) == site.resolve()

    def test_env_var(self, site, tmp_path):
        assert vs.resolve_site(env={"SRE_HARNESS_SITE": str(site)}, home=tmp_path) == site.resolve()

    def test_pointer_file(self, site, tmp_path):
        pointer = tmp_path / ".config" / "sre-harness" / "site"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(f"{site}\n", encoding="utf-8")
        assert vs.resolve_site(env={}, home=tmp_path) == site.resolve()

    def test_pointer_symlink(self, site, tmp_path):
        pointer = tmp_path / ".config" / "sre-harness" / "site"
        pointer.parent.mkdir(parents=True)
        pointer.symlink_to(site, target_is_directory=True)
        assert vs.resolve_site(env={}, home=tmp_path) == site.resolve()

    def test_nothing_configured_fails_loudly(self, tmp_path):
        with pytest.raises(vs.SiteError, match="no site configured"):
            vs.resolve_site(env={}, home=tmp_path)

    def test_directory_without_site_yaml_fails(self, tmp_path):
        with pytest.raises(vs.SiteError, match="no site.yaml"):
            vs.resolve_site(str(tmp_path), env={}, home=tmp_path)


class TestMain:
    def test_valid_site_exits_zero(self, site, capsys):
        assert vs.main(["--site", str(site)]) == 0
        assert "valid" in capsys.readouterr().out

    def test_invalid_site_exits_one(self, edit_site, capsys):
        site = edit_site(lambda doc: doc.pop("audit"))
        assert vs.main(["--site", str(site), "--json"]) == 1
        assert '"ok": false' in capsys.readouterr().out

    def test_unresolved_site_exits_two(self, tmp_path, capsys):
        assert vs.main(["--site", str(tmp_path / "missing")]) == 2
        assert "not a directory" in capsys.readouterr().err
