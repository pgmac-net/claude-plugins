"""Tests for budget.py: the Tier 2 gate, the Nagios check and daily snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import audit_hook
import autonomy
import budget
import pytest
import slo
import sre_common
import yaml
from sre_common import SiteError
from test_slo import JOURNEY, SLO_SETTINGS, probe_slo, site_doc

SERVICE = "component:example-service"
OTHER = "component:other-service"
OWNER = "user:example-owner"


@pytest.fixture
def budget_site(edit_site):
    """A site with one budgeted SLO on example-service, whose journey also suspends other-service."""

    def mutate(doc):
        doc["slo"] = dict(SLO_SETTINGS)

    root = edit_site(mutate)
    other = {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Component",
        "metadata": {"name": "other-service"},
        "spec": {"type": "service", "lifecycle": "production", "owner": OWNER, "sre": {"tier": 3, "purpose": "x"}},
    }
    (root / "catalog" / "services" / "other-service.yaml").write_text(yaml.safe_dump(other), encoding="utf-8")
    folder = root / "catalog" / "journeys" / "example-service"
    folder.mkdir(parents=True)
    journey = JOURNEY.replace("error_budget_policy:", f"suspends: [{OTHER}]\nerror_budget_policy:")
    (folder / "open-app.md").write_text(journey, encoding="utf-8")
    (folder / "error-budget-policy.md").write_text("# Policy\n", encoding="utf-8")
    (folder / f"open-app{slo.SLO_SUFFIX}").write_text(yaml.safe_dump(probe_slo()), encoding="utf-8")
    return root


def backend(err7d: float | None, err1h: float = 0.0, calls: list | None = None):
    """A stand-in for slo.query answering the status query."""

    def fake_query(_settings, promql, _token, at=None):  # pylint: disable=unused-argument
        if calls is not None:
            calls.append(promql)
        if err7d is None:
            return []
        return [
            {"metric": {"__name__": "slo:sli_error:ratio_rate7d", "slo": "example-open-app"}, "value": [0, str(err7d)]},
            {"metric": {"__name__": "slo:sli_error:ratio_rate1h", "slo": "example-open-app"}, "value": [0, str(err1h)]},
        ]

    return fake_query


def gate_for(root: Path) -> budget.BudgetGate:
    return budget.BudgetGate(root, site_doc(root), token_reader=lambda _settings: "token")


# --- journeys: suspends ------------------------------------------------------------


def test_journey_suspends_must_name_catalog_services(budget_site):
    path = budget_site / "catalog" / "journeys" / "example-service" / "open-app.md"
    path.write_text(path.read_text().replace(OTHER, "component:not-there"), encoding="utf-8")
    found = slo.load(budget_site, site_doc(budget_site), {SERVICE, OTHER})
    assert any("component:not-there is not in the catalog" in error for error in found.errors)


def test_status_reports_freeze_scope_and_whether_frozen(budget_site, monkeypatch):
    monkeypatch.setattr(slo, "query", backend(0.006))
    found = slo.load(budget_site, site_doc(budget_site))
    [result] = slo.status(SLO_SETTINGS, found.slos, "token", found.journeys)
    assert result["state"] == "exhausted"
    assert result["frozen"] is True
    assert result["freeze_scope"] == [SERVICE, OTHER]


# --- the gate ----------------------------------------------------------------------


def test_site_without_slos_never_suspends(site):
    assert budget.BudgetGate(site, site_doc(site)).reason(SERVICE) is None


def test_exhausted_budget_suspends_its_service_and_what_it_names(budget_site, monkeypatch):
    monkeypatch.setattr(slo, "query", backend(0.006))
    gate = gate_for(budget_site)
    assert "exhausted" in gate.reason(SERVICE)
    assert "exhausted" in gate.reason(OTHER)
    assert gate.reason("component:unrelated") is None


def test_healthy_budget_suspends_nothing_and_reads_once(budget_site, monkeypatch):
    calls: list = []
    monkeypatch.setattr(slo, "query", backend(0.001, calls=calls))
    gate = gate_for(budget_site)
    assert gate.reason(SERVICE) is None
    assert gate.reason(OTHER) is None
    assert len(calls) == 1


def test_burning_fast_does_not_suspend(budget_site, monkeypatch):
    monkeypatch.setattr(slo, "query", backend(0.001, err1h=0.5))
    assert gate_for(budget_site).reason(SERVICE) is None


def test_unknown_budget_fails_closed(budget_site, monkeypatch):
    monkeypatch.setattr(slo, "query", backend(None))
    assert "unknown state" in gate_for(budget_site).reason(SERVICE)


def test_unreadable_backend_fails_closed(budget_site, monkeypatch):
    def broken(*_args, **_kwargs):
        raise SiteError("metrics backend answered HTTP 503")

    monkeypatch.setattr(slo, "query", broken)
    reason = gate_for(budget_site).reason(SERVICE)
    assert "could not be read" in reason and "HTTP 503" in reason


def test_missing_token_fails_closed(budget_site):
    def no_token(_settings):
        raise SiteError("no metrics read token")

    gate = budget.BudgetGate(budget_site, site_doc(budget_site), token_reader=no_token)
    assert "no metrics read token" in gate.reason(SERVICE)
    assert gate.reason("component:unrelated") is None


def test_invalid_slos_fail_closed_for_every_service(budget_site):
    (budget_site / "catalog" / "journeys" / "example-service" / "open-app.md").write_text("no front matter")
    assert "not valid" in gate_for(budget_site).reason("component:unrelated")


# --- decide with the gate -----------------------------------------------------------

ACTION = {
    "id": "say",
    "service": SERVICE,
    "command": "echo restarted {name}",
    "parameters": {"name": {"pattern": "[a-z0-9-]+"}},
    "precondition": "It is safe to say so.",
    "max_per_incident": 2,
}
ALLOW = {"schema_version": 1, "tier2_enabled": True, "actions": [ACTION]}


def test_decide_keeps_a_suspended_action_at_tier_one():
    decision = autonomy.decide(ALLOW, "echo restarted x", None, None, gate=lambda _service: "budget is exhausted")
    assert decision["tier"] == 1
    assert decision["budget_suspended"] is True
    assert decision["reason"] == "budget is exhausted"


def test_decide_asks_the_gate_only_about_a_tier_two_match():
    asked: list = []

    def gate(service):
        asked.append(service)

    assert autonomy.decide(ALLOW, "echo something else entirely", None, None, gate=gate)["tier"] == 1
    assert asked == []
    assert autonomy.decide(ALLOW, "echo restarted x", None, None, gate=gate)["tier"] == 2
    assert asked == [SERVICE]


def test_match_records_the_suspension_in_the_audit_log(budget_site, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SRE_HARNESS_SITE", str(budget_site))
    monkeypatch.setenv("TEST_METRICS_TOKEN", "token-for-tests")
    monkeypatch.setattr(slo, "query", backend(0.006))
    (budget_site / "autonomy.yaml").write_text(yaml.safe_dump(ALLOW), encoding="utf-8")
    sre_common.write_incident_state({"id": "catalog-5", "repo": "o/r", "number": 5, "url": "u", "title": "T"})

    assert autonomy.main(["match", "--command", "echo restarted x"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["tier"] == 1 and "exhausted" in decision["reason"]
    records = [json.loads(line) for line in audit_hook.spool_path("catalog-5").read_text().splitlines()]
    assert [(r["event"], r["action"], r["service"]) for r in records] == [("tier2_suspended", "say", SERVICE)]
    assert "exhausted" in records[0]["reason"]


# --- the Nagios check --------------------------------------------------------------


def result(state: str, **extra) -> dict:
    base = {
        "slo": "shop",
        "state": state,
        "attainment": 0.999,
        "budget_remaining": 0.8,
        "budget_remaining_minutes": 40.3,
        "burn_rate": {"1h": 0.5, "6h": 0.25},
        "spends_budget": True,
        "frozen": state == "exhausted",
        "freeze_scope": [SERVICE, OTHER],
    }
    return {**base, **extra}


@pytest.mark.parametrize(
    ("state", "code", "word"),
    [("healthy", 0, "OK"), ("burning fast", 1, "WARNING"), ("exhausted", 2, "CRITICAL"), ("informational", 0, "OK")],
)
def test_nagios_levels(state, code, word):
    got, line = budget.nagios([result(state)])
    assert got == code
    assert line.startswith(f"SLO {word} - shop")


def test_nagios_unknown_and_empty_are_unknown():
    assert budget.nagios([{"slo": "shop", "state": "unknown", "reason": "no data"}])[0] == 3
    assert budget.nagios([])[0] == 3


def test_nagios_names_the_freeze_and_has_clean_perfdata():
    _code, line = budget.nagios([result("exhausted", budget_remaining=-0.2, budget_remaining_minutes=-10.1)])
    text, perf = line.split(" | ")
    assert "FREEZE example-service, other-service" in text
    assert perf.split() == ["shop_budget=-0.2000;;0:", "shop_burn_1h=0.50;14.4;;0;", "shop_burn_6h=0.25;6;;0;"]


def test_nagios_worst_state_wins_and_unknown_outranks_warning():
    assert budget.nagios([result("healthy"), result("burning fast", slo="b")])[0] == 1
    assert budget.nagios([result("burning fast"), {"slo": "b", "state": "unknown"}])[0] == 3
    assert budget.nagios([{"slo": "b", "state": "unknown"}, result("exhausted")])[0] == 2


def test_nagios_output_is_ascii_and_never_splits_perfdata():
    _code, line = budget.nagios([{"slo": "shop", "state": "unknown", "reason": "bad | thing – here"}])
    assert line.isascii()
    assert "|" not in line


def test_nagios_command_reports_problems_as_unknown(budget_site, monkeypatch, capsys):
    monkeypatch.delenv("TEST_METRICS_TOKEN", raising=False)
    assert slo.main(["--site", str(budget_site), "nagios"]) == 3
    assert capsys.readouterr().out.startswith("SLO UNKNOWN - no metrics read token")


def test_nagios_command_end_to_end(budget_site, monkeypatch, capsys):
    monkeypatch.setenv("TEST_METRICS_TOKEN", "token-for-tests")
    monkeypatch.setattr(slo, "query", backend(0.006))
    assert slo.main(["--site", str(budget_site), "nagios", "--slo", "example-open-app"]) == 2
    assert "FREEZE example-service, other-service" in capsys.readouterr().out


# --- snapshots ----------------------------------------------------------------------

TZ = "Australia/Brisbane"
TODAY = date(2026, 12, 3)


def history_backend(no_data: set[date] | None = None, exhausted: set[date] | None = None, calls: list | None = None):
    """Answers the snapshot queries. Each day is identified by the local day its end time closes."""

    def fake_query(_settings, promql, _token, at=None):
        day = datetime.fromtimestamp(at, tz=ZoneInfo(TZ)).date() - timedelta(days=1)
        if calls is not None:
            calls.append((day, promql))
        if day in (no_data or set()):
            return []
        if promql.startswith("min_over_time(slo:error_budget"):
            value = -0.1 if day in (exhausted or set()) else 0.7
        elif promql.startswith("slo:error_budget"):
            value = 0.75
        elif promql.startswith("max_over_time"):
            value = 0.001
        else:
            value = 0.002
        return [{"metric": {"slo": "example-open-app"}, "value": [at, str(value)]}]

    return fake_query


def run_snapshot(root: Path, history: Path) -> dict:
    found = slo.load(root, site_doc(root))
    return budget.snapshot(history, SLO_SETTINGS, found, "token", timezone=TZ, today=TODAY)


def lines(history: Path) -> list[dict]:
    return budget.read_history(history)


def test_first_run_backfills_every_day_inside_retention(budget_site, monkeypatch, tmp_path):
    monkeypatch.setattr(slo, "query", history_backend())
    history = tmp_path / "history"
    history.mkdir()
    result = run_snapshot(budget_site, history)
    days = [row["date"] for row in lines(history)]
    # Retention is 14 days: the 13 complete days before today, oldest first, across two month files.
    assert result["written"] == 13
    assert days[0] == "2026-11-20" and days[-1] == "2026-12-02"
    assert sorted(path.name for path in history.iterdir()) == ["2026-11.jsonl", "2026-12.jsonl"]
    last = lines(history)[-1]
    assert last["captured"] == "on time"
    assert lines(history)[0]["captured"] == "backfilled"
    assert last["attainment_1d"] == 0.998
    assert last["state"] == "healthy"
    assert last["freeze_scope"] == [SERVICE, OTHER]


def test_rerun_writes_only_missing_days_and_keeps_what_was_written(budget_site, monkeypatch, tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(slo, "query", history_backend(no_data={date(2026, 11, 25)}))
    first = run_snapshot(budget_site, history)
    assert first["no_data"] == ["2026-11-25 example-open-app"]
    before = (history / "2026-11.jsonl").read_text()

    calls: list = []
    monkeypatch.setattr(slo, "query", history_backend(calls=calls))
    second = run_snapshot(budget_site, history)
    assert second["written"] == 1
    assert {day for day, _promql in calls} == {date(2026, 11, 25)}
    after = (history / "2026-11.jsonl").read_text()
    assert set(before.splitlines()) < set(after.splitlines())


def test_a_day_whose_budget_ran_out_is_exhausted_and_frozen(budget_site, monkeypatch, tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(slo, "query", history_backend(exhausted={date(2026, 12, 1)}))
    run_snapshot(budget_site, history)
    row = next(row for row in lines(history) if row["date"] == "2026-12-01")
    assert (row["state"], row["frozen"], row["budget_remaining_min"]) == ("exhausted", True, -0.1)


def test_snapshot_queries_each_day_at_its_local_end(budget_site, monkeypatch, tmp_path):
    seen: list = []

    def record(_settings, promql, _token, at=None):
        seen.append(at)
        return history_backend()(_settings, promql, _token, at=at)

    monkeypatch.setattr(slo, "query", record)
    history = tmp_path / "history"
    history.mkdir()
    run_snapshot(budget_site, history)
    # 2026-12-02 ends at local midnight in Brisbane, 14:00 UTC the day before.
    assert datetime(2026, 12, 2, 14, tzinfo=ZoneInfo("UTC")).timestamp() in seen


def test_snapshot_command_needs_an_existing_directory(budget_site, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TEST_METRICS_TOKEN", "token-for-tests")
    assert slo.main(["--site", str(budget_site), "snapshot", "--out", str(tmp_path / "nope")]) == 2
    assert "does not exist" in capsys.readouterr().err
