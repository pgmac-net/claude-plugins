"""Tests for catalog_sync.py, using small source repositories built in tmp_path."""

from __future__ import annotations

import shutil
from pathlib import Path

import catalog_sync as cs
import pytest
import yaml

INVENTORY = """\
# comment
[web]
web01.example.com ansible_user=deploy
web02.example.com   # trailing comment

[db]
db01 ansible_password=should-never-appear

[db:vars]
secret_var=should-never-appear

[laptops]
laptop01

[servers:children]
web
db
"""

NAGIOS = """\
define host {
    use         linux-server        ; template
    host_name   web01
    address     192.0.2.11
    hostgroups  web-servers
    parents     edge
}
define host {
    host_name   db01
    address     192.0.2.12
}
define host {
    host_name   edge
    address     192.0.2.1
}
define host {
    name        linux-server
    register    0
}
define hostgroup {
    hostgroup_name  web-servers
}
define hostgroup {
    hostgroup_name  all-servers
    members         db01
    hostgroup_members web-servers
}
define service {
    service_description Disk
    hostgroup_name      all-servers
    check_command       check_disk!--password=should-never-appear
}
define service {
    service_description HTTP
    hostgroup_name      web-servers
    check_command       check_http
}
define service {
    service_description Ping
    host_name           *,!edge
}
"""

APPLICATION = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
spec:
  project: default
  destination:
    namespace: {namespace}
  source:
    repoURL: https://charts.example.com/
    chart: {name}
    targetRevision: 1.2.3
  syncPolicy:
    automated: {{}}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: not-an-application
"""


@pytest.fixture
def estate(tmp_path: Path) -> tuple[Path, dict]:
    """A site root plus three source checkouts, and the matching site document."""
    site = tmp_path / "site"
    (site / "catalog").mkdir(parents=True)
    ansible = tmp_path / "ansible"
    (ansible / "inventory").mkdir(parents=True)
    (ansible / "inventory" / "hosts.ini").write_text(INVENTORY, encoding="utf-8")
    nagios = tmp_path / "nagios"
    (nagios / "objects").mkdir(parents=True)
    (nagios / "objects" / "hosts.cfg").write_text(NAGIOS, encoding="utf-8")
    gitops = tmp_path / "gitops"
    (gitops / "apps" / "plain").mkdir(parents=True)
    (gitops / "apps" / "plain" / "shop.yaml").write_text(
        APPLICATION.format(name="shop", namespace="shop"), encoding="utf-8"
    )
    (gitops / "apps" / "plain" / "web01.yaml").write_text(
        APPLICATION.format(name="blog", namespace="web"), encoding="utf-8"
    )

    def repo(path: Path) -> dict:
        return {"repo": f"example-org/{path.name}", "visibility": "private", "path": str(path)}

    doc = {
        "repos": {
            "catalog": {"repo": "example-org/site", "visibility": "private"},
            "ansible": repo(ansible),
            "nagios": repo(nagios),
            "gitops": repo(gitops),
        },
        "catalog": {
            "path": "catalog",
            "default_owner": "user:owner",
            "sources": [
                {"name": "checks", "type": "nagios-objects", "repo": "nagios", "path": "objects"},
                {
                    "name": "hosts",
                    "type": "ansible-inventory",
                    "repo": "ansible",
                    "path": "inventory/hosts.ini",
                    "options": {"exclude_groups": ["laptops"]},
                },
                {
                    "name": "apps",
                    "type": "argocd-applications",
                    "repo": "gitops",
                    "path": "apps",
                    "pattern": "argocd-helm-app",
                    "options": {"cluster": "main"},
                },
            ],
        },
    }
    return site, doc


def entity(build: cs.Build, rel: str) -> dict:
    return yaml.safe_load(build.files[rel])


def test_inventory_reads_hosts_and_groups_but_never_variables(tmp_path):
    path = tmp_path / "hosts.ini"
    path.write_text(INVENTORY, encoding="utf-8")
    hosts, warnings = cs.read_ansible_inventory(path)
    assert warnings == []
    assert hosts["web01.example.com"] == {"web", "servers"}
    assert hosts["db01"] == {"db", "servers"}
    assert set(hosts) == {"web01.example.com", "web02.example.com", "db01", "laptop01"}


def test_build_produces_expected_entities(estate):
    site, doc = estate
    build = cs.build(site, doc)
    assert sorted(build.files) == [
        "apps/app-blog.yaml",
        "apps/app-shop.yaml",
        "apps/cluster-main.yaml",
        "checks/monitored-edge.yaml",
        "hosts/host-db01.yaml",
        "hosts/host-web01.yaml",
        "hosts/host-web02.yaml",
        "sources.yaml",
    ]
    assert build.counts == {"apps": 3, "checks": 1, "hosts": 3}


def test_no_variable_or_check_command_reaches_the_catalog(estate):
    site, doc = estate
    build = cs.build(site, doc)
    assert all("should-never-appear" not in text for text in build.files.values())
    assert all("check_command" not in text for text in build.files.values())


def test_excluded_group_is_skipped(estate):
    site, doc = estate
    assert "hosts/host-laptop01.yaml" not in cs.build(site, doc).files


def test_nagios_facts_merge_onto_the_matching_host(estate):
    site, doc = estate
    web01 = entity(cs.build(site, doc), "hosts/host-web01.yaml")
    monitoring = web01["spec"]["sre"]["monitoring"]
    assert monitoring["host"] == "web01"
    assert monitoring["hostgroups"] == ["all-servers", "web-servers"]
    assert monitoring["checks"] == ["Disk", "HTTP", "Ping"]
    assert web01["spec"]["dependsOn"] == ["resource:monitored-edge"]
    assert web01["spec"]["sre"]["groups"] == ["servers", "web"]


def test_exclusion_and_wildcard_in_service_host_name(estate):
    site, doc = estate
    build = cs.build(site, doc)
    assert entity(build, "checks/monitored-edge.yaml")["spec"]["sre"]["monitoring"]["checks"] == []
    assert entity(build, "hosts/host-db01.yaml")["spec"]["sre"]["monitoring"]["checks"] == ["Disk", "Ping"]


def test_unmonitored_host_has_no_monitoring_block(estate):
    site, doc = estate
    assert "monitoring" not in entity(cs.build(site, doc), "hosts/host-web02.yaml")["spec"]["sre"]


def test_application_resource(estate):
    site, doc = estate
    shop = entity(cs.build(site, doc), "apps/app-shop.yaml")
    assert shop["spec"]["type"] == "argocd-application"
    assert shop["spec"]["owner"] == "user:owner"
    assert shop["spec"]["dependsOn"] == ["resource:cluster-main"]
    assert shop["spec"]["sre"] == {
        "application": "shop",
        "area": "plain",
        "namespace": "shop",
        "project": "default",
        "automated_sync": True,
        "sources": [{"repo_url": "https://charts.example.com/", "chart": "shop", "target_revision": "1.2.3"}],
        "pattern": "argocd-helm-app",
    }


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_helm_chart_area_is_rendered(estate):
    site, doc = estate
    chart = Path(doc["repos"]["gitops"]["path"]) / "apps" / "charted"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: charted\nversion: 0.1.0\n", encoding="utf-8")
    (chart / "values.yaml").write_text("ns: rendered\n", encoding="utf-8")
    template = APPLICATION.format(name="wiki", namespace="{{ .Values.ns }}").replace("automated: {}", "{}")
    (chart / "templates" / "wiki.yaml").write_text(template, encoding="utf-8")
    wiki = entity(cs.build(site, doc), "apps/app-wiki.yaml")
    assert wiki["spec"]["sre"]["namespace"] == "rendered"
    assert wiki["spec"]["sre"]["area"] == "charted"
    assert wiki["spec"]["sre"]["automated_sync"] is False


def test_unparseable_area_warns_and_is_skipped(estate):
    site, doc = estate
    bad = Path(doc["repos"]["gitops"]["path"]) / "apps" / "broken"
    bad.mkdir()
    (bad / "x.yaml").write_text("spec: {{ .Values.nope }}\n  bad: [", encoding="utf-8")
    build = cs.build(site, doc)
    assert any("broken" in warning for warning in build.warnings)
    assert "apps/app-shop.yaml" in build.files


def test_build_is_deterministic(estate):
    site, doc = estate
    assert cs.build(site, doc).files == cs.build(site, doc).files


def test_write_then_check_is_clean_and_detects_drift(estate):
    site, doc = estate
    generated = site / "catalog" / "generated"
    build = cs.build(site, doc)
    cs.write_generated(generated, build.files)
    assert cs.differences(build.files, cs.read_generated(generated)) == []

    (generated / "hosts" / "host-web01.yaml").write_text("edited by hand\n", encoding="utf-8")
    (generated / "hosts" / "stray.yaml").write_text("x: 1\n", encoding="utf-8")
    (generated / "apps" / "app-shop.yaml").unlink()
    assert cs.differences(build.files, cs.read_generated(generated)) == [
        "missing: apps/app-shop.yaml",
        "unexpected: hosts/stray.yaml",
        "changed: hosts/host-web01.yaml",
    ]

    cs.write_generated(generated, build.files)
    assert cs.differences(build.files, cs.read_generated(generated)) == []


def test_write_never_touches_files_outside_generated(estate):
    site, doc = estate
    service = site / "catalog" / "services" / "mine.yaml"
    service.parent.mkdir(parents=True)
    service.write_text("hand-written\n", encoding="utf-8")
    cs.write_generated(site / "catalog" / "generated", cs.build(site, doc).files)
    assert service.read_text(encoding="utf-8") == "hand-written\n"


def test_missing_checkout_is_a_site_error(estate):
    site, doc = estate
    doc["repos"]["ansible"]["path"] = str(site / "nowhere")
    with pytest.raises(cs.SiteError, match="checkout not found"):
        cs.build(site, doc)


def test_main_modes(estate, capsys):
    site, doc = estate
    doc = {"schema_version": 2, **doc}
    (site / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert cs.main(["--site", str(site), "--check"]) == 1
    assert "stale" in capsys.readouterr().out
    assert cs.main(["--site", str(site)]) == 0
    assert cs.main(["--site", str(site), "--check"]) == 0
    assert "current" in capsys.readouterr().out
