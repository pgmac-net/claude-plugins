"""Tests for report.py: the monthly reliability report."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import incident
import pytest
import report
import sre_common
import yaml
from test_budget import OTHER, SERVICE, make_budget_site

MONTH = "2026-11"
YESTERDAY = date(2026, 12, 2)


def tier_one(root: Path, name: str, resources: list[str] | None = None) -> None:
    """Make a catalog service Tier 1 (the template's tier 1 requires an SLA report)."""
    path = root / "catalog" / "services" / f"{name}.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["spec"]["sre"]["tier"] = 1
    if resources:
        doc["spec"]["dependsOn"] = [f"resource:{r}" for r in resources]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def snapshot_line(day: str, state: str = "healthy", attainment: float = 0.999, low: float = 0.7, **extra) -> dict:
    return {
        "date": day,
        "slo": "example-open-app",
        "service": SERVICE,
        "spends_budget": True,
        "attainment_1d": attainment,
        "error_ratio_1d": round(1 - attainment, 6),
        "budget_remaining": low,
        "budget_remaining_min": low,
        "state": state,
        "data_minutes": 1440,
        "frozen": state == "exhausted",
        "freeze_scope": [SERVICE, OTHER],
        **extra,
    }


def write_history(history: Path, rows: list[dict]) -> None:
    history.mkdir(exist_ok=True)
    by_month: dict[str, list[str]] = {}
    for row in rows:
        by_month.setdefault(row["date"][:7], []).append(json.dumps(row))
    for month, lines in by_month.items():
        (history / f"{month}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def issue(number: int, created: str, services: str | None = None, affected: str = "x", **extra) -> dict:
    body = incident.BODY.format(
        marker=incident.MARKER,
        clock="10:00 AEST",
        utc=created,
        severity="P2",
        phase="Resolved",
        affected=affected,
        services=services or incident.NOT_KNOWN,
        impact="i",
    )
    return {
        "number": number,
        "title": f"Incident {number}",
        "body": body,
        "createdAt": created,
        "closedAt": None,
        "url": f"https://example.com/i/{number}",
        "state": "CLOSED",
        **extra,
    }


@pytest.fixture
def tracker(monkeypatch):
    issues: list[dict] = []

    def fake_gh(args, stdin=None, timeout=None):  # pylint: disable=unused-argument
        assert args[:2] == ["issue", "list"]
        return json.dumps(issues)

    monkeypatch.setattr(sre_common, "run_gh", fake_gh)
    return issues


@pytest.fixture
def report_site(edit_site):
    root = make_budget_site(edit_site)
    tier_one(root, "example-service", resources=["host-box"])
    return edit_site(lambda doc: doc["incident"].update(timezone="Australia/Brisbane"))


def build(root: Path, history: Path) -> tuple[str, int]:
    doc = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))
    month = report.parse_month(MONTH, doc["incident"]["timezone"])
    return report.build(root, doc, month, history, YESTERDAY)


def full_month(overrides: dict[int, dict] | None = None) -> list[dict]:
    return [snapshot_line(f"2026-11-{day:02d}", **(overrides or {}).get(day, {})) for day in range(1, 31)]


def test_report_sections_and_figures(report_site, tracker, tmp_path):
    history = tmp_path / "history"
    write_history(history, full_month({15: {"state": "exhausted", "low": -0.2}, 16: {"state": "burning fast"}}))
    tracker.append(issue(7, "2026-11-15T00:00:00Z", services="component:example-service"))
    text, missing = build(report_site, history)

    assert text.startswith("# Reliability report: November 2026\n")
    assert missing == 0
    assert "### example-service" in text
    assert "30 of 30 day(s) captured since data began on 2026-11-01" in text
    assert "Lowest budget remaining: -20% on 2026-11-15." in text
    assert "Days burning fast: 1. Days exhausted: 1." in text
    assert "Days frozen, with Tier 2 off: 1." in text
    assert "| [#7](https://example.com/i/7) Incident 7 | P2 |" in text
    assert "budget exhausted on 1 day(s)" in text
    assert "| example-service | `example-open-app` | 99.5% |" in text


def test_attainment_is_the_mean_of_daily_values(report_site, tracker, tmp_path):
    history = tmp_path / "history"
    write_history(history, [snapshot_line("2026-11-01", attainment=1.0), snapshot_line("2026-11-02", attainment=0.99)])
    text, _missing = build(report_site, history)
    assert "Attainment: **99.500%**, the mean of 2 daily value(s)" in text
    assert tracker == []


def test_coverage_counts_from_the_first_snapshot_and_reports_gaps(report_site, tracker, tmp_path):
    history = tmp_path / "history"
    rows = [snapshot_line(f"2026-11-{day:02d}") for day in range(20, 31) if day not in (24, 25)]
    write_history(history, rows)
    text, missing = build(report_site, history)
    assert missing == 2
    assert "9 of 11 day(s) captured since data began on 2026-11-20" in text
    assert "snapshots missing for 2 day(s)" in text
    assert tracker == []


def test_service_without_a_journey_is_never_shown_healthy(report_site, tracker, tmp_path):
    tier_one(report_site, "other-service")
    tracker.append(issue(8, "2026-11-03T00:00:00Z", services="component:other-service"))
    write_history(tmp_path / "history", full_month())
    text, _missing = build(report_site, tmp_path / "history")
    section = text.split("### other-service", 1)[1].split("###", 1)[0]
    assert "No journey is defined for this service, so nothing is measured." in section
    assert "1 incident(s) this month and nothing measured" in section
    assert "| other-service | none defined |" in text


def test_attribution_falls_back_to_affected_and_keeps_the_unmatched(report_site, tracker, tmp_path):
    tracker += [
        issue(1, "2026-11-02T00:00:00Z", affected="host-box (disk full); everything else fine"),
        issue(2, "2026-11-03T00:00:00Z", affected="media apps"),
        issue(3, "2026-10-30T00:00:00Z", services="component:example-service"),
        issue(4, "2026-11-04T00:00:00Z", title="[DRILL] Practice"),
        {**issue(5, "2026-11-05T00:00:00Z"), "body": "a runbook gap", "state": "OPEN", "title": "Runbook gap"},
    ]
    write_history(tmp_path / "history", full_month())
    text, _missing = build(report_site, tmp_path / "history")
    service = text.split("### example-service", 1)[1].split("## Unattributed", 1)[0]
    assert "[#1]" in service and "| Affected |" in service
    unattributed = text.split("## Unattributed incidents", 1)[1].split("##", 1)[0]
    assert "[#2]" in unattributed and "incident.py set --services" in unattributed
    assert "[#3]" not in text, "opened last month"
    assert "[#4]" not in text, "drills are not incidents"
    assert "- [#5](https://example.com/i/5) Runbook gap" in text.split("## Other open issues", 1)[1]
    assert "3 incident(s)" not in text and "2 incident(s) opened this month; 1 unattributed." in text


def test_incident_opened_near_midnight_counts_in_the_local_month(report_site, tracker, tmp_path):
    # 2026-10-31T15:00Z is 01:00 on 1 November in Brisbane.
    tracker.append(issue(9, "2026-10-31T15:00:00Z", services="component:example-service"))
    write_history(tmp_path / "history", full_month())
    text, _missing = build(report_site, tmp_path / "history")
    assert "[#9]" in text


def test_well_above_target_all_month_suggests_tightening(report_site, tracker, tmp_path):
    write_history(tmp_path / "history", [snapshot_line(f"2026-11-{d:02d}", attainment=1.0) for d in range(1, 31)])
    text, _missing = build(report_site, tmp_path / "history")
    assert "Consider a tighter target" in text
    assert tracker == []


def test_command_writes_the_file_and_fails_when_too_much_is_missing(report_site, tracker, tmp_path, capsys):
    history = tmp_path / "history"
    write_history(history, [snapshot_line("2026-11-01")])
    out = tmp_path / "reports" / "2026-11.md"
    args = ["--site", str(report_site), "--month", MONTH, "--history", str(history), "--out", str(out)]
    assert report.main(args) == 1
    assert out.read_text(encoding="utf-8").startswith("# Reliability report: November 2026")
    assert "snapshots are missing" in capsys.readouterr().err
    assert tracker == []


def test_bad_month_is_refused(report_site, tmp_path, capsys):
    (tmp_path / "h").mkdir()
    assert report.main(["--site", str(report_site), "--month", "November", "--history", str(tmp_path / "h")]) == 2
    assert "--month must be YYYY-MM" in capsys.readouterr().err


def test_partly_measured_days_are_said_so(report_site, tracker, tmp_path):
    write_history(tmp_path / "history", [snapshot_line("2026-11-01", data_minutes=43), snapshot_line("2026-11-02")])
    text, _missing = build(report_site, tmp_path / "history")
    assert "1 of those day(s) were only partly measured" in text
    assert tracker == []
