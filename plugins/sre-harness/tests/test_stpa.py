"""Tests for stpa.py: checking, drawing and listing findings of STPA analyses."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import stpa
import validate_site
import yaml

ISSUE = "https://github.com/example-org/ops/issues/7"

VALID: dict = {
    "schema_version": 1,
    "service": "component:example-service",
    "status": "reviewed",
    "reviewed": "2026-11-01",
    "losses": [{"id": "L-1", "text": "Users lose data"}],
    "hazards": [{"id": "H-1", "text": "Volume is read-only while in use", "losses": ["L-1"]}],
    "controllers": [
        {"id": "C-1", "name": "Operator", "kind": "human", "responsibilities": ["Restore volumes"]},
        {
            "id": "C-2",
            "name": "Self-heal handler",
            "kind": "automated",
            "responsibilities": ["Remount stale volumes"],
        },
    ],
    "processes": [{"id": "P-1", "name": "Volume"}],
    "control_actions": [
        {"id": "CA-1", "from": "C-2", "to": "P-1", "action": "Unmount and remount"},
        {"id": "CA-2", "from": "C-1", "to": "C-2", "action": "Enable or disable the handler"},
    ],
    "feedback": [
        {"id": "F-1", "from": "P-1", "to": "C-2", "signal": "Mount state check", "status": "present"},
        {"id": "F-2", "from": "C-2", "to": "C-1", "signal": "Handler outcome", "status": "missing"},
    ],
    "ucas": [
        {
            "id": "UCA-1",
            "action": "CA-1",
            "type": "provided-unsafe",
            "context": "while a pod still writes",
            "hazards": ["H-1"],
            "evidence": ["PIR 2026-07-01-example"],
        },
        {
            "id": "UCA-2",
            "action": "CA-2",
            "type": "not-provided",
            "context": "handler misfires repeatedly",
            "hazards": ["H-1"],
            "hypothesised": True,
        },
    ],
    "scenarios": [
        {"id": "LS-1", "ucas": ["UCA-1"], "text": "Guard sees no writer", "status": "controlled", "control": "Guard"},
        {
            "id": "LS-2",
            "ucas": ["UCA-2"],
            "text": "Operator never learns the handler failed",
            "status": "uncontrolled",
            "causal_factors": ["missing-feedback"],
            "finding": ISSUE,
        },
    ],
}


def write(site: Path, meta: dict, name: str = "example-service", body: str = "# Example\n\nText.\n") -> Path:
    folder = site / "analyses" / "stpa"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body, encoding="utf-8")
    return path


def load(site: Path) -> list[stpa.Analysis]:
    doc = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    return stpa.load_analyses(site, doc, stpa.catalog_services(site, doc))


def only(found: list[stpa.Analysis]) -> stpa.Analysis:
    assert len(found) == 1
    return found[0]


def variant(**changes) -> dict:
    meta = copy.deepcopy(VALID)
    meta.update(changes)
    return meta


def test_valid_analysis_has_no_errors(site):
    write(site, VALID)
    one = only(load(site))
    assert one.errors == []
    assert one.reviewed.isoformat() == "2026-11-01"


def test_dates_stay_strings_for_the_schema(site):
    path = write(site, VALID)
    meta, body = stpa.split_front_matter(path.read_text(encoding="utf-8"))
    assert meta["reviewed"] == "2026-11-01"
    assert body.startswith("# Example")


def test_dangling_references_are_errors(site):
    meta = variant()
    meta["control_actions"][0]["to"] = "P-9"
    meta["ucas"][0]["hazards"] = ["H-4"]
    meta["scenarios"][0]["ucas"] = ["UCA-9"]
    write(site, meta)
    errors = " ".join(load(site)[0].errors)
    assert "P-9 is not a defined controller or process" in errors
    assert "H-4 is not a defined hazard" in errors
    assert "UCA-9 is not a defined unsafe control action" in errors


def test_duplicate_ids_are_errors(site):
    meta = variant()
    meta["losses"].append({"id": "L-1", "text": "Again"})
    write(site, meta)
    assert any("L-1 is used more than once" in e for e in load(site)[0].errors)


@pytest.mark.parametrize(
    "change",
    [{"evidence": [], "hypothesised": False}, {"hypothesised": True}],
    ids=["neither", "both"],
)
def test_a_uca_cites_evidence_or_says_it_is_hypothesised(site, change):
    meta = variant()
    meta["ucas"][0].update(change)
    write(site, meta)
    assert any("UCA-1: cite evidence" in e for e in load(site)[0].errors)


def test_uncontrolled_without_issue_is_an_error_once_reviewed_and_a_warning_in_draft(site):
    meta = variant()
    del meta["scenarios"][1]["finding"]
    write(site, meta)
    assert any("LS-2: uncontrolled" in e for e in load(site)[0].errors)
    write(site, {**meta, "status": "draft"})
    one = load(site)[0]
    assert not one.errors
    assert any("LS-2: uncontrolled" in w for w in one.warnings)


def test_controlled_scenario_must_name_its_control(site):
    meta = variant()
    del meta["scenarios"][0]["control"]
    write(site, meta)
    assert any("LS-1: a controlled scenario must name its control" in e for e in load(site)[0].errors)


def test_coverage_warnings(site):
    meta = variant()
    meta["control_actions"].append({"id": "CA-3", "from": "C-1", "to": "P-1", "action": "Restore from backup"})
    meta["hazards"].append({"id": "H-2", "text": "Backups are stale", "losses": ["L-1"]})
    meta["feedback"][0]["status"] = "missing"
    meta["proposed"] = [{"kind": "uca", "text": "Handler acts on a remounted volume", "source": "PIR x"}]
    write(site, meta)
    warnings = " ".join(load(site)[0].warnings)
    assert "CA-3: no unsafe control action considered" in warnings
    assert "H-2: no unsafe control action leads to this hazard" in warnings
    assert "C-2: automated controller 'Self-heal handler' receives no working feedback" in warnings
    assert "1 proposed change(s)" in warnings


def test_file_name_and_catalog_must_match(site):
    write(site, variant(service="component:nowhere"), name="example-service")
    errors = " ".join(load(site)[0].errors)
    assert "name it nowhere.md" in errors
    assert "component:nowhere is not in the catalog" in errors


def test_schema_errors_stop_further_checks(site):
    write(site, variant(status="finished"))
    one = only(load(site))
    assert one.errors and all("status" in e for e in one.errors)


def test_render_adds_then_rewrites_the_diagram(site):
    path = write(site, VALID)
    one = only(load(site))
    assert stpa.render(one)
    text = path.read_text(encoding="utf-8")
    assert "## Control structure" in text and "```mermaid" in text
    assert 'C2 -->|"CA-1: Unmount and remount"| P1' in text
    assert "(MISSING)" in text and "linkStyle 3 stroke:#d93f0b" in text
    again = only(load(site))
    assert not again.warnings or all("diagram" not in w for w in again.warnings)
    assert not stpa.render(again)

    meta = variant()
    meta["processes"][0]["name"] = "Block volume"
    path.write_text(path.read_text(encoding="utf-8").replace("name: Volume", "name: Block volume"), encoding="utf-8")
    stale = only(load(site))
    assert any("diagram is out of date" in w for w in stale.warnings)
    assert stpa.render(stale)
    assert path.read_text(encoding="utf-8").count("```mermaid") == 1


def test_findings_lists_uncontrolled_scenarios(site):
    write(site, VALID)
    found = stpa.findings(load(site)[0].meta)
    assert [f["id"] for f in found] == ["LS-2"]
    assert found[0]["hazards"] == ["H-1"] and found[0]["finding"] == ISSUE


def test_settings_fill_defaults():
    assert stpa.settings({})["theme_min_pirs"] == 3
    assert stpa.settings({"reliability": {"theme_min_pirs": 5}})["theme_min_pirs"] == 5


def test_commands(site, capsys):
    write(site, VALID)
    assert stpa.main(["--site", str(site), "check", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"] == {"analyses": 1, "reviewed": 1, "uncontrolled scenarios": 1}
    assert stpa.main(["--site", str(site), "findings", "example-service"]) == 0
    assert ISSUE in capsys.readouterr().out
    assert stpa.main(["--site", str(site), "render", "component:example-service"]) == 0
    assert "rewrote" in capsys.readouterr().out
    assert stpa.main(["--site", str(site), "render", "missing"]) == 2


def test_validate_site_reports_analysis_errors(site):
    meta = variant()
    meta["ucas"][0]["action"] = "CA-9"
    write(site, meta)
    report = validate_site.validate(site)
    assert any("CA-9 is not a defined control action" in e for e in report.errors)
    assert report.summary["stpa_analyses"] == ["example-service (reviewed)"]


def test_template_site_has_no_analyses_and_validates(site):
    report = validate_site.validate(site)
    assert report.ok, report.errors
    assert report.summary["stpa_analyses"] == []
