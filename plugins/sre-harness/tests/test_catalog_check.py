"""Tests for catalog_check.py."""

from __future__ import annotations

from pathlib import Path

import catalog_check as cc
import pytest
import yaml

TIERS = {
    "schema_version": 1,
    "tiers": [
        {
            "id": 1,
            "name": "critical",
            "description": "x",
            "requires": ["catalog_entry"],
            "catalog_requires": ["dependencies", "pattern", "monitoring", "runbooks"],
        },
        {"id": 3, "name": "best-effort", "description": "x", "requires": ["catalog_entry"]},
    ],
}


def resource(name: str, checks: list[str] | None = None, depends: list[str] | None = None) -> dict:
    spec: dict = {"type": "host", "owner": "user:owner"}
    if depends:
        spec["dependsOn"] = depends
    spec["sre"] = {"monitoring": {"checks": checks}} if checks is not None else {}
    return {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Resource",
        "metadata": {"name": name, "annotations": {cc.GENERATED_ANNOTATION: "hosts"}},
        "spec": spec,
    }


def service(name: str, tier: int = 1, depends: list[str] | None = None, **sre) -> dict:
    spec: dict = {"type": "service", "lifecycle": "production", "owner": "user:owner"}
    if depends:
        spec["dependsOn"] = depends
    spec["sre"] = {"tier": tier, **sre}
    return {"apiVersion": "backstage.io/v1alpha1", "kind": "Component", "metadata": {"name": name}, "spec": spec}


OWNER = {"apiVersion": "backstage.io/v1alpha1", "kind": "User", "metadata": {"name": "owner"}, "spec": {"memberOf": []}}


class Site:
    """A throwaway site with a catalog, patterns and runbooks."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog = root / "catalog"
        (self.catalog / "generated" / "hosts").mkdir(parents=True)
        (self.catalog / "services").mkdir()
        (root / "patterns").mkdir()
        (root / "patterns" / "compose.md").write_text("# Compose\n", encoding="utf-8")
        self.runbooks = root / "incidents" / "runbooks"
        self.runbooks.mkdir(parents=True)
        (self.runbooks / "disk-full.md").write_text("# Disk full\n", encoding="utf-8")
        self.doc = {
            "repos": {
                "catalog": {"repo": "o/site", "visibility": "private"},
                "incidents": {
                    "repo": "o/inc",
                    "visibility": "private",
                    "path": str(root / "incidents"),
                    "runbooks_path": "runbooks",
                },
            },
            "catalog": {"path": "catalog", "default_owner": "user:owner", "runbooks_repo": "incidents"},
            "patterns_path": "patterns",
        }
        self.write("owners/owner.yaml", OWNER)

    def write(self, rel: str, doc: dict | str) -> None:
        path = self.catalog / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc if isinstance(doc, str) else yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    def check(self) -> cc.CatalogReport:
        return cc.check(self.root, self.doc, TIERS)


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


def complete_service(**overrides) -> dict:
    fields = {"depends": ["resource:host-web01"], "pattern": "compose", "runbooks": ["disk-full.md"]}
    fields.update(overrides)
    return service("shop", **fields)


def test_complete_tier_one_service_is_clean(site):
    site.write("generated/hosts/host-web01.yaml", resource("host-web01", checks=["Disk"]))
    site.write("services/shop.yaml", complete_service())
    report = site.check()
    assert report.errors == []
    assert report.warnings == []
    assert report.summary == {
        "services": 1,
        "services_by_tier": {"1": 1},
        "systems": 0,
        "generated_resources": 1,
        "uncatalogued": 0,
        "unverified_services": 0,
    }


def test_schema_violation_is_reported_with_its_file(site):
    bad = service("shop")
    bad["spec"]["sre"]["priority"] = "high"
    site.write("services/shop.yaml", bad)
    assert any(err.startswith("services/shop.yaml: spec/sre") for err in site.check().errors)


def test_dangling_reference_is_an_error(site):
    site.write("services/shop.yaml", complete_service(depends=["resource:gone"]))
    assert any("resource:gone, which does not exist (dangling)" in err for err in site.check().errors)


def test_repeated_dangling_reference_is_reported_once(site):
    for index in range(5):
        doc = resource(f"host-{index}")
        doc["spec"]["owner"] = "user:nobody"
        site.write(f"generated/hosts/host-{index}.yaml", doc)
    errors = [err for err in site.check().errors if "user:nobody" in err]
    assert len(errors) == 1
    assert errors[0].startswith("5 entities (first: generated/hosts/host-0.yaml)")


def test_duplicate_entity_is_an_error(site):
    site.write("services/shop.yaml", service("shop", tier=3))
    site.write("services/shop-copy.yaml", service("shop", tier=3))
    assert any("component:shop is already defined" in err for err in site.check().errors)


def test_uncatalogued_resource_warns_and_reachability_is_transitive(site):
    site.write(
        "generated/hosts/host-web01.yaml", resource("host-web01", checks=["Disk"], depends=["resource:host-hyper"])
    )
    site.write("generated/hosts/host-hyper.yaml", resource("host-hyper"))
    site.write("generated/hosts/host-spare.yaml", resource("host-spare"))
    site.write("services/shop.yaml", complete_service())
    report = site.check()
    assert report.errors == []
    assert report.warnings == ["uncatalogued: resource:host-spare belongs to no service and is not in ignore.yaml"]
    assert report.summary["uncatalogued"] == 1


def test_ignore_list_silences_and_goes_stale(site):
    site.write("generated/hosts/host-web01.yaml", resource("host-web01", checks=["Disk"]))
    site.write("generated/hosts/host-spare.yaml", resource("host-spare"))
    site.write("services/shop.yaml", complete_service())
    site.write(
        "ignore.yaml",
        {
            "ignore": [
                {"ref": "resource:host-spare", "reason": "scratch"},
                {"ref": "resource:host-gone", "reason": "old"},
                {"ref": "resource:host-web01", "reason": "was unused"},
            ]
        },
    )
    report = site.check()
    assert report.summary["uncatalogued"] == 0
    assert report.warnings == [
        "ignore.yaml: resource:host-gone no longer exists; remove the entry",
        "ignore.yaml: resource:host-web01 is now part of a service; remove the entry",
    ]


def test_ignore_entry_needs_a_reason(site):
    site.write("ignore.yaml", {"ignore": [{"ref": "resource:x", "reason": " "}]})
    assert any("needs a 'ref' and a 'reason'" in err for err in site.check().errors)


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"depends": None}, "dependencies"),
        ({"pattern": None}, "pattern"),
        ({"runbooks": None}, "runbooks"),
    ],
)
def test_missing_tier_requirement_is_an_error_unless_unverified(site, overrides, missing):
    site.write("generated/hosts/host-web01.yaml", resource("host-web01", checks=["Disk"]))
    fields = {
        k: v
        for k, v in {
            "depends": ["resource:host-web01"],
            "pattern": "compose",
            "runbooks": ["disk-full.md"],
            **overrides,
        }.items()
        if v
    }
    explicit = {} if "depends" in fields else {"monitoring": ["web01: Disk"]}
    site.write("services/shop.yaml", service("shop", **fields, **explicit))
    assert any(f"{missing} required at tier 1 but missing" in err for err in site.check().errors)

    site.write("services/shop.yaml", service("shop", **fields, **explicit, unverified=[missing]))
    report = site.check()
    assert not any("required at tier 1" in err for err in report.errors)
    assert any(f"{missing} required at tier 1 but not yet known (unverified)" in w for w in report.warnings)


def test_monitoring_can_be_derived_or_explicit(site):
    site.write("generated/hosts/host-web01.yaml", resource("host-web01", checks=[]))
    site.write("services/shop.yaml", complete_service())
    assert any("monitoring required at tier 1 but missing" in err for err in site.check().errors)
    site.write("services/shop.yaml", complete_service(monitoring=["edge: HTTP shop"]))
    assert site.check().errors == []


def test_drafted_facts_warn_until_confirmed(site):
    site.write("generated/hosts/host-web01.yaml", resource("host-web01", checks=["Disk"]))
    site.write("services/shop.yaml", complete_service(unverified=["dependencies", "purpose"]))
    report = site.check()
    assert report.errors == []
    assert report.warnings == ["services/shop.yaml: drafted but not confirmed by a person: dependencies, purpose"]
    assert report.summary["unverified_services"] == 1


def test_unknown_tier_pattern_and_runbook(site):
    site.write("services/a.yaml", service("a", tier=7))
    site.write("services/b.yaml", service("b", tier=3, pattern="nope", runbooks=["missing.md"]))
    site.write("services/c.yaml", service("c", tier=3))
    report = site.check()
    assert any("tier 7 is not defined" in err for err in report.errors)
    assert any("pattern 'nope' has no page" in err for err in report.errors)
    assert any("runbook not found: missing.md" in w for w in report.warnings)
    assert any("services/c.yaml: no deployment pattern stated" in w for w in report.warnings)


def test_layer_placement_is_enforced(site):
    site.write("generated/hosts/mine.yaml", service("mine", tier=3, pattern="compose"))
    site.write("services/host-copy.yaml", resource("host-copy"))
    errors = site.check().errors
    assert any("hand-written entity inside generated/" in err for err in errors)
    assert any("generated entity outside generated/" in err for err in errors)


def test_template_catalog_is_valid():
    plugin = Path(__file__).resolve().parent.parent
    assert cc.main(["--site", str(plugin / "templates" / "site-repo")]) == 0
