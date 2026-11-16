"""Tests for the generated SLO dashboard."""

from __future__ import annotations

import json

import slo
import slo_dashboard
from test_slo import REFS, counter_slo, site_doc, slo_site, write_slo  # noqa: F401  pylint: disable=unused-import


def make(name="x", target=0.995, budget=True, counter=False):
    return slo.Slo(
        name, "svc", "j", target, "7d", counter, 'probe_success{job="p"}', 'probe_success{job="p"}', budget, 60, "p"
    )


def exprs(panels):
    return [target["expr"] for panel in panels for target in panel.get("targets", [])]


def test_budgeted_slo_row_reads_only_recorded_series_and_its_probe():
    model = slo_dashboard.build([make("ingress-open")])
    panels = model["panels"]
    titles = [p["title"] for p in panels]
    assert titles[0].startswith("ingress-open: 99.50% over 7d (spends budget)")
    assert "Budget left (minutes)" in titles
    assert "Burn rate (1h)" in titles and "Burn rate (6h)" in titles
    for expr in exprs(panels):
        assert expr.startswith(("slo:", "1 - slo:", 'probe_success{job="p"}')), expr
    budget = next(p for p in panels if p["title"] == "Budget left (minutes)")
    assert budget["targets"][0]["expr"] == 'slo:error_budget:remaining_ratio{slo="ingress-open"} * 50.4'


def test_informational_slo_has_no_budget_panels_and_comes_after_budgeted_ones():
    model = slo_dashboard.build([make("requests", budget=False, counter=True), make("probe")])
    rows = [p["title"] for p in model["panels"] if p["type"] == "row"]
    assert rows[0].startswith("probe:")
    assert "informational" in rows[1]
    requests_panels = model["panels"][[p["title"] for p in model["panels"]].index(rows[1]) :]
    assert not any(p["title"].startswith(("Budget", "Burn")) for p in requests_panels)
    assert any("sum(rate(" in e for e in exprs(requests_panels))


def test_ids_unique_and_layout_does_not_overlap():
    model = slo_dashboard.build([make("a"), make("b"), make("c", budget=False)])
    ids = [p["id"] for p in model["panels"]]
    assert len(ids) == len(set(ids))
    cells = set()
    for panel in model["panels"]:
        grid = panel["gridPos"]
        for x in range(grid["x"], grid["x"] + grid["w"]):
            for y in range(grid["y"], grid["y"] + grid["h"]):
                assert (x, y) not in cells, panel["title"]
                cells.add((x, y))


def test_model_is_stable_and_uses_a_datasource_variable():
    first = slo_dashboard.render([make("a")])
    assert first == slo_dashboard.render([make("a")])
    model = json.loads(first)
    assert model["uid"] == "sre-harness-slos"
    assert model["editable"] is False
    assert all(p["datasource"]["uid"] == "${datasource}" for p in model["panels"] if "datasource" in p)


def test_dashboard_command_writes_then_checks(slo_site, capsys):  # noqa: F811
    path = slo_site / "site.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "rules_file: slo/rules.yaml", "rules_file: slo/rules.yaml\n  dashboard_file: slo/dashboard.json"
    )
    path.write_text(text, encoding="utf-8")
    assert slo.main(["--site", str(slo_site), "dashboard", "--check"]) == 1
    assert slo.main(["--site", str(slo_site), "dashboard"]) == 0
    model = json.loads((slo_site / "slo" / "dashboard.json").read_text(encoding="utf-8"))
    assert model["panels"][0]["title"].startswith("example-open-app")
    assert slo.main(["--site", str(slo_site), "dashboard", "--check"]) == 0
    write_slo(slo_site, counter_slo(), name="requests")
    assert slo.main(["--site", str(slo_site), "dashboard", "--check"]) == 1
    capsys.readouterr()


def test_dashboard_command_needs_the_setting(slo_site, capsys):  # noqa: F811
    assert slo.main(["--site", str(slo_site), "dashboard"]) == 2
    assert "dashboard_file" in capsys.readouterr().err
