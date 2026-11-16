"""Tests for slo.py: journeys, OpenSLO checks, generated rules, status maths."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import slo
import validate_site as vs
import yaml
from sre_common import SiteError

SLO_SETTINGS = {
    "backend": "prometheus",
    "url": "https://metrics.example.com/api/prom",
    "user": "12345",
    "token_env": "TEST_METRICS_TOKEN",
    "retention_days": 14,
    "journeys_path": "catalog/journeys",
    "rules_file": "slo/rules.yaml",
}

JOURNEY = """---
title: Open the example app
service: component:example-service
owner: user:example
steps:
  - name: Resolve the name
  - name: Reach the app
    depends_on: [component:example-service]
    measured_by: canary probe
error_budget_policy: error-budget-policy.md
---

# Open the example app

Prose for people.
"""


def probe_slo(**changes) -> dict:
    doc = {
        "apiVersion": "openslo/v1",
        "kind": "SLO",
        "metadata": {
            "name": "example-open-app",
            "annotations": {"sre-harness/journey": "open-app", "sre-harness/expected-interval": "60s"},
        },
        "spec": {
            "service": "example-service",
            "budgetingMethod": "Occurrences",
            "timeWindow": [{"duration": "7d", "isRolling": True}],
            "objectives": [{"target": 0.995}],
            "indicator": {
                "metadata": {"name": "canary-probe"},
                "spec": {
                    "ratioMetric": {
                        "counter": False,
                        "good": {
                            "metricSource": {"type": "Prometheus", "spec": {"query": 'probe_success{job="canary"}'}}
                        },
                        "total": {
                            "metricSource": {"type": "Prometheus", "spec": {"query": 'probe_success{job="canary"}'}}
                        },
                    }
                },
            },
        },
    }
    for path, value in changes.items():
        node = doc
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[int(key)] if key.isdigit() else node[key]
        node[keys[-1]] = value
    return doc


def counter_slo() -> dict:
    doc = probe_slo()
    doc["metadata"] = {
        "name": "example-requests",
        "annotations": {"sre-harness/journey": "open-app", "sre-harness/budget": "false"},
    }
    doc["spec"]["indicator"]["spec"]["ratioMetric"] = {
        "counter": True,
        "good": {"metricSource": {"type": "Prometheus", "spec": {"query": 'requests_total{status!~"5.."}'}}},
        "total": {"metricSource": {"type": "Prometheus", "spec": {"query": "requests_total"}}},
    }
    return doc


@pytest.fixture
def slo_site(edit_site):
    def mutate(doc):
        doc["slo"] = dict(SLO_SETTINGS)

    root = edit_site(mutate)
    folder = root / "catalog" / "journeys" / "example-service"
    folder.mkdir(parents=True)
    (folder / "open-app.md").write_text(JOURNEY, encoding="utf-8")
    (folder / "error-budget-policy.md").write_text("# Policy\n", encoding="utf-8")
    (folder / f"open-app{slo.SLO_SUFFIX}").write_text(yaml.safe_dump(probe_slo()), encoding="utf-8")
    return root


def site_doc(root: Path) -> dict:
    return yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))


def write_slo(root: Path, doc: dict, name: str = "open-app") -> None:
    path = root / "catalog" / "journeys" / "example-service" / f"{name}{slo.SLO_SUFFIX}"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


REFS = {"component:example-service"}


# --- loading and checking ------------------------------------------------------


def test_valid_site_has_one_journey_and_one_slo(slo_site):
    found = slo.load(slo_site, site_doc(slo_site), REFS)
    assert found.errors == []
    assert [j.name for j in found.journeys] == ["open-app"]
    loaded = found.slos[0]
    assert (loaded.name, loaded.target, loaded.window, loaded.counter, loaded.interval) == (
        "example-open-app",
        0.995,
        "7d",
        False,
        60,
    )


def test_whole_site_validates_and_counts_journeys(slo_site):
    report = vs.validate(slo_site)
    assert report.errors == []
    assert report.summary["slo"]["journeys"] == 1
    assert report.summary["slo"]["slos"] == 1


def test_empty_slo_section_means_nothing_to_check(site):
    assert vs.validate(site).summary["slo"] is None


def test_slo_files_are_not_mistaken_for_catalog_entities(slo_site):
    assert not any("open-app.slo.yaml" in err for err in vs.validate(slo_site).errors)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"spec.timeWindow": [{"duration": "28d", "isRolling": True}]}, "longer than the backend keeps data"),
        ({"spec.timeWindow": [{"duration": "6h", "isRolling": True}]}, "at least 1d"),
        ({"spec.objectives": [{"target": 0.5}]}, "must be at least 0.9"),
        ({"spec.timeWindow": [{"duration": "7d", "isRolling": False}]}, "isRolling"),
        ({"metadata.annotations": {"sre-harness/journey": "open-app"}}, "expected-interval"),
        (
            {"metadata.annotations": {"sre-harness/journey": "nowhere", "sre-harness/expected-interval": "60s"}},
            "no journey file",
        ),
        ({"spec.service": "other-service"}, "does not match its directory"),
    ],
)
def test_bad_slos_are_errors(slo_site, changes, message):
    write_slo(slo_site, probe_slo(**changes))
    errors = slo.load(slo_site, site_doc(slo_site), REFS).errors
    assert any(message in err for err in errors), errors


@pytest.mark.parametrize(
    "query",
    ["sum(rate(requests_total[5m]))", 'up{job="x"} or vector(1)', "up{job=`x`}", 'up{job="$x"}'],
)
def test_queries_must_be_plain_selectors(slo_site, query):
    doc = probe_slo()
    doc["spec"]["indicator"]["spec"]["ratioMetric"]["good"]["metricSource"]["spec"]["query"] = query
    write_slo(slo_site, doc)
    errors = slo.load(slo_site, site_doc(slo_site), REFS).errors
    assert any("plain selector" in err for err in errors)


def test_journey_must_name_real_catalog_entries(slo_site):
    errors = slo.load(slo_site, site_doc(slo_site), {"component:somewhere-else"}).errors
    assert any("component:example-service is not in the catalog" in err for err in errors)


def test_journey_needs_its_policy_file(slo_site):
    (slo_site / "catalog" / "journeys" / "example-service" / "error-budget-policy.md").unlink()
    errors = slo.load(slo_site, site_doc(slo_site), REFS).errors
    assert any("error_budget_policy" in err for err in errors)


def test_journey_without_front_matter_is_an_error(slo_site):
    (slo_site / "catalog" / "journeys" / "example-service" / "second.md").write_text("# no front matter\n")
    assert any("no YAML front matter" in err for err in slo.load(slo_site, site_doc(slo_site), REFS).errors)


def test_duplicate_slo_names_are_an_error(slo_site):
    write_slo(slo_site, probe_slo(), name="copy")
    assert any("used more than once" in err for err in slo.load(slo_site, site_doc(slo_site), REFS).errors)


def test_unmeasured_journey_is_a_warning(slo_site):
    (slo_site / "catalog" / "journeys" / "example-service" / f"open-app{slo.SLO_SUFFIX}").unlink()
    found = slo.load(slo_site, site_doc(slo_site), REFS)
    assert found.errors == []
    assert any("no SLO measures this journey" in w for w in found.warnings)


def test_tier_one_service_without_journeys_is_a_warning_not_an_error(edit_site):
    def mutate(doc):
        doc["slo"] = dict(SLO_SETTINGS)

    root = edit_site(mutate)
    (root / "catalog" / "journeys").mkdir()
    entity = root / "catalog" / "services" / "example-service.yaml"
    data = yaml.safe_load(entity.read_text(encoding="utf-8"))
    data["spec"]["sre"]["tier"] = 1
    entity.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = vs.validate(root)
    assert not any("requires journeys" in err for err in report.errors)
    assert any("component:example-service: tier 1 requires journeys" in w for w in report.warnings)
    assert report.summary["slo"]["services_needing_journeys"] == 1
    assert report.summary["slo"]["services_with_journeys"] == 0


# --- recording rules -------------------------------------------------------------


def test_probe_rules_count_missing_samples_as_failure(slo_site):
    found = slo.load(slo_site, site_doc(slo_site), REFS)
    group = slo.rule_group(found.slos[0])
    base = group["rules"][0]
    assert base["record"] == "slo:sli_error:ratio_rate5m"
    assert base["expr"].endswith("or on() vector(1)")
    assert base["expr"] == '1 - avg(avg_over_time(probe_success{job="canary"}[5m])) or on() vector(1)'
    assert "sum_over_time" not in base["expr"], "counting samples against an expected number reads jitter as failure"
    assert base["labels"] == {
        "slo": "example-open-app",
        "service": "example-service",
        "journey": "open-app",
        "budget": "true",
    }
    records = [rule["record"] for rule in group["rules"]]
    assert records == [
        "slo:sli_error:ratio_rate5m",
        "slo:sli_error:ratio_rate1h",
        "slo:sli_error:ratio_rate6h",
        "slo:sli_error:ratio_rate1d",
        "slo:sli_error:ratio_rate7d",
        "slo:objective:ratio",
        "slo:error_budget:remaining_ratio",
    ]
    assert group["rules"][-1]["expr"] == '1 - slo:sli_error:ratio_rate7d{slo="example-open-app"} / 0.005'


def test_counter_rules_treat_no_traffic_as_no_errors(slo_site):
    write_slo(slo_site, counter_slo(), name="requests")
    found = slo.load(slo_site, site_doc(slo_site), REFS)
    rule = slo.rule_group(next(s for s in found.slos if s.counter))["rules"][0]
    assert "and on() (sum(rate(requests_total[5m])) > 0) or on() vector(0)" in rule["expr"]
    assert rule["labels"]["budget"] == "false"


def test_generate_writes_then_check_passes_then_detects_staleness(slo_site, capsys):
    assert slo.main(["--site", str(slo_site), "generate", "--check"]) == 1
    assert slo.main(["--site", str(slo_site), "generate"]) == 0
    rules = (slo_site / "slo" / "rules.yaml").read_text(encoding="utf-8")
    assert rules.startswith(slo.RULES_HEADER)
    doc = yaml.safe_load(rules)
    assert doc["namespace"] == "sre-harness"
    assert doc["groups"][0]["name"] == "sre-harness-slo-example-open-app"
    assert "&id" not in rules and "*id" not in rules, "a generated file must be plain YAML, no anchors"
    assert slo.main(["--site", str(slo_site), "generate", "--check"]) == 0
    write_slo(slo_site, probe_slo(**{"spec.objectives": [{"target": 0.99}]}))
    assert slo.main(["--site", str(slo_site), "generate", "--check"]) == 1
    capsys.readouterr()


def test_generate_refuses_invalid_slos(slo_site):
    write_slo(slo_site, probe_slo(**{"spec.objectives": [{"target": 0.5}]}))
    assert slo.main(["--site", str(slo_site), "generate"]) == 1
    assert not (slo_site / "slo" / "rules.yaml").exists()


def test_check_command(slo_site, capsys):
    assert slo.main(["--site", str(slo_site), "check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["slos"] == 1


# --- status ------------------------------------------------------------------


def make_slo(target: float = 0.995, budget: bool = True) -> slo.Slo:
    return slo.Slo("x", "svc", "j", target, "7d", False, "up", "up", budget, 60, "p")


def values(err7d: float, err1h: float = 0.0, err6h: float = 0.0) -> dict:
    return {
        "slo:sli_error:ratio_rate7d": err7d,
        "slo:sli_error:ratio_rate1h": err1h,
        "slo:sli_error:ratio_rate6h": err6h,
    }


def test_healthy():
    result = slo.assess(make_slo(), values(0.001))
    assert result["state"] == "healthy"
    assert result["attainment"] == 0.999
    assert result["budget_remaining"] == 0.8
    assert result["budget_remaining_minutes"] == 40.3


def test_exhausted():
    assert slo.assess(make_slo(), values(0.006))["state"] == "exhausted"


def test_burning_fast_on_one_hour_burn():
    result = slo.assess(make_slo(), values(0.001, err1h=0.08))
    assert result["state"] == "burning fast"
    assert result["burn_rate"]["1h"] == 16.0


def test_no_data_is_unknown_not_healthy():
    assert slo.assess(make_slo(), {})["state"] == "unknown"


def test_informational_slo_has_no_budget_state():
    assert slo.assess(make_slo(budget=False), values(0.5))["state"] == "informational"


def test_status_queries_once_and_maps_results(monkeypatch, slo_site):
    seen = []

    def fake_query(_settings, promql, _token):
        seen.append(promql)
        return [
            {"metric": {"__name__": "slo:sli_error:ratio_rate7d", "slo": "example-open-app"}, "value": [0, "0.001"]},
            {"metric": {"__name__": "slo:sli_error:ratio_rate1h", "slo": "example-open-app"}, "value": [0, "0"]},
        ]

    monkeypatch.setattr(slo, "query", fake_query)
    found = slo.load(slo_site, site_doc(slo_site), REFS)
    result = slo.status(SLO_SETTINGS, found.slos, "token")
    assert len(seen) == 1
    # No backslashes: '\-' is an invalid escape in a PromQL string, and the backend answers 400.
    assert 'slo=~"example-open-app"' in seen[0]
    assert "\\" not in seen[0]
    assert result[0]["state"] == "healthy"


def test_status_command_needs_a_token(slo_site, monkeypatch, capsys):
    monkeypatch.delenv("TEST_METRICS_TOKEN", raising=False)
    assert slo.main(["--site", str(slo_site), "status"]) == 2
    assert "no metrics read token" in capsys.readouterr().err


def test_token_from_env_then_file(tmp_path):
    (tmp_path / "sre-harness").mkdir()
    token_file = tmp_path / "sre-harness" / "token"
    token_file.write_text("token-from-file\n", encoding="utf-8")
    token_file.chmod(0o600)
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    settings = {"token_env": "X_TOKEN", "token_file": "token"}
    assert slo.read_token(settings, {**env, "X_TOKEN": "token-from-env"}) == "token-from-env"
    assert slo.read_token(settings, env) == "token-from-file"
    with pytest.raises(SiteError):
        slo.read_token({"token_env": "X_TOKEN"}, env)


def test_query_error_never_contains_the_token(monkeypatch):
    def refuse(request, **_kwargs):
        raise slo.urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(slo, "_OPENER", SimpleNamespace(open=refuse))
    with pytest.raises(SiteError) as error:
        slo.query(SLO_SETTINGS, "up", "very-secret-token")
    assert "HTTP 401" in str(error.value)
    assert "very-secret-token" not in str(error.value)
