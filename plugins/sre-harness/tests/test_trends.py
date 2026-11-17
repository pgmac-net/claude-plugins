"""Tests for trends.py: the recommendations backlog ranked from PIRs and action items."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import sre_common
import trends
import yaml
from test_stpa import VALID, write

AS_OF = date(2026, 11, 30)


def pir(folder: Path, stem: str, **meta) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    head = {"title": stem, "date": stem[:10], "severity": "P2", **meta}
    (folder / f"{stem}.md").write_text("---\n" + yaml.safe_dump(head) + "---\n# PIR\n", encoding="utf-8")


def gh_issue(number: int, created: str, body: str = "", state: str = "OPEN", labels=()):
    repo = "example-org/ops"
    return {
        "number": number,
        "title": f"Item {number}",
        "url": f"https://github.com/{repo}/issues/{number}",
        "state": state,
        "createdAt": created,
        "closedAt": None if state == "OPEN" else created,
        "labels": [{"name": name} for name in labels],
        "repository": {"nameWithOwner": repo},
        "body": body,
    }


@pytest.fixture
def gh(monkeypatch):
    state = {"issues": [], "unreadable": set(), "search_fails": False, "calls": []}

    def fake(args, stdin=None, timeout=None):  # pylint: disable=unused-argument
        state["calls"].append(args)
        if args[0] == "api":
            if args[1].removeprefix("repos/") in state["unreadable"]:
                raise sre_common.SiteError("gh api failed: HTTP 404")
            return "ok"
        assert args[:2] == ["search", "issues"]
        if state["search_fails"]:
            raise sre_common.SiteError("gh search issues failed: HTTP 403")
        label = args[args.index("--label") + 1]
        return json.dumps([i for i in state["issues"] if label in [x["name"] for x in i["labels"]]])

    monkeypatch.setattr(sre_common, "run_gh", fake)
    return state


@pytest.fixture
def tsite(edit_site):
    def mutate(doc):
        doc["incident"]["timezone"] = "Australia/Brisbane"
        doc["repos"]["ops"] = {"repo": "example-org/ops", "visibility": "private"}

    root = edit_site(mutate)
    path = root / "catalog" / "services" / "example-service.yaml"
    entity = yaml.safe_load(path.read_text(encoding="utf-8"))
    entity["spec"]["sre"]["tier"] = 1
    entity["spec"]["dependsOn"] = ["resource:host-box"]
    path.write_text(yaml.safe_dump(entity), encoding="utf-8")
    return root


def build(root: Path, pirs: Path) -> trends.Backlog:
    doc = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))
    return trends.build(root, doc, AS_OF, str(pirs))


def test_services_line_wins_then_tags_then_unattributed(tsite, tmp_path, gh):  # pylint: disable=unused-argument
    folder = tmp_path / "pirs"
    pir(folder, "2026-10-01-a", services=["example-service"], tags=["other"])
    pir(folder, "2026-10-02-b", tags=["host-box", "nfs"])
    pir(folder, "2026-10-03-c", tags=["nfs"])
    pir(folder, "2026-10-04-d", services=["ghost"])
    backlog = build(tsite, folder)
    assert [p.stem for p in backlog.unattributed] == ["2026-10-03-c", "2026-10-04-d"]
    assert any("names 'ghost', which is not a catalog service" in n for n in backlog.notes)


def test_themes_respect_window_threshold_and_ignored_tags(tsite, tmp_path, gh, edit_site):  # pylint: disable=unused-argument
    edit_site(lambda doc: doc.update(reliability={"theme_min_pirs": 2, "ignore_tags": ["sre-harness"]}))
    folder = tmp_path / "pirs"
    pir(folder, "2026-01-01-old", services=["example-service"], tags=["nfs"])  # outside 180 days
    pir(folder, "2026-09-01-a", services=["example-service"], tags=["nfs", "sre-harness"])
    pir(folder, "2026-11-01-b", services=["example-service"], tags=["nfs", "sre-harness", "example-service"])
    backlog = build(tsite, folder)
    assert [(t.name, t.count, t.latest.isoformat()) for t in backlog.service_themes] == [
        ("component:example-service", 2, "2026-11-01")
    ]
    assert [t.name for t in backlog.tag_themes] == ["nfs"]
    assert backlog.pirs_in_window == 2


def test_stale_items_rank_by_priority_then_age_and_link_services(tsite, tmp_path, gh):
    folder = tmp_path / "pirs"
    pir(folder, "2026-08-01-a", services=["example-service"])
    gh["issues"] += [
        gh_issue(1, "2026-07-01T00:00:00Z", labels=["incident", "priority:low"]),
        gh_issue(2, "2026-08-02T00:00:00Z", body="From .../src/2026-08-01-a.md", labels=["incident", "priority:high"]),
        gh_issue(3, "2026-06-01T00:00:00Z", labels=["design-review"], body="analyses/stpa/example-service.md"),
        gh_issue(4, "2026-11-20T00:00:00Z", labels=["incident"]),  # too new
        gh_issue(5, "2026-01-01T00:00:00Z", labels=["incident"], state="CLOSED"),
    ]
    backlog = build(tsite, folder)
    assert [(i.number, i.priority) for i in backlog.stale] == [(2, "high"), (1, "low"), (3, "none")]
    assert backlog.stale[0].services == ["component:example-service"]
    assert backlog.stale[2].services == ["component:example-service"]
    assert backlog.items_read == 5


def test_repeat_incidents_need_two_pirs_and_an_open_item(tsite, tmp_path, gh):
    folder = tmp_path / "pirs"
    pir(folder, "2026-09-01-a", services=["example-service"])
    pir(folder, "2026-10-01-b", services=["example-service"])
    assert build(tsite, folder).repeats == []
    gh["issues"].append(gh_issue(9, "2026-10-02T00:00:00Z", body="x/2026-10-01-b.md", labels=["incident"]))
    [(ref, count, items)] = build(tsite, folder).repeats
    assert (ref, count, [i.number for i in items]) == ("component:example-service", 2, [9])


def test_design_review_due_for_a_tier_one_theme_without_analysis(tsite, tmp_path, gh):  # pylint: disable=unused-argument
    folder = tmp_path / "pirs"
    for day in ("2026-09-01", "2026-10-01", "2026-11-01"):
        pir(folder, f"{day}-x", services=["example-service"])
    [(ref, reasons)] = build(tsite, folder).reviews_due
    assert ref == "component:example-service"
    assert reasons == ["recurring theme (3 PIRs) and no STPA analysis"]


def test_design_review_due_after_three_pirs_since_review_and_for_proposals(tsite, tmp_path, gh):  # pylint: disable=unused-argument
    folder = tmp_path / "pirs"
    write(tsite, VALID)
    for day in ("2026-10-01", "2026-11-02", "2026-11-03"):
        pir(folder, f"{day}-x", services=["example-service"])
    assert build(tsite, folder).reviews_due == []
    pir(folder, "2026-11-04-x", services=["example-service"])
    meta = {**VALID, "proposed": [{"kind": "uca", "text": "New unsafe action", "source": "PIR y"}]}
    write(tsite, meta)
    [(_, reasons)] = build(tsite, folder).reviews_due
    assert reasons == ["3 PIRs since the last review on 2026-11-01", "1 change(s) proposed by PIRs wait for review"]


def test_unreadable_sources_are_said_not_skipped(tsite, tmp_path, gh):
    gh["unreadable"].add("example-org/ops")
    backlog = build(tsite, tmp_path / "missing")
    notes = " ".join(backlog.notes)
    assert "PIRs not read" in notes
    assert "private repositories the token cannot see: ops (example-org/ops)" in notes
    gh["search_fails"] = True
    assert any("action items not read: gh search issues failed" in n for n in build(tsite, tmp_path).notes)
    section = "\n".join(trends.render_section(backlog))
    assert "**Not complete:**" in section


def test_render_section_has_every_heading(tsite, tmp_path, gh):
    folder = tmp_path / "pirs"
    pir(folder, "2026-11-01-a", tags=["nfs"])
    gh["issues"].append(gh_issue(1, "2026-01-01T00:00:00Z", labels=["incident"]))
    text = "\n".join(trends.render_section(build(tsite, folder), level=2))
    for heading in (
        "## Recommendations backlog",
        "### Recurring themes",
        "### Stale action items",
        "### Repeat incidents with open action items",
        "### Design reviews due",
        "### Unattributed PIRs",
    ):
        assert heading in text
    assert "[example-org/ops#1](https://github.com/example-org/ops/issues/1) Item 1 | none | 2026-01-01" in text
    assert "https://github.com/example-org/incidents/blob/HEAD/docs/incidents/2026-11-01-a.md" in text


def test_search_covers_every_owner_and_label(tsite, tmp_path, gh):
    build(tsite, tmp_path)
    searches = [(c[c.index("--owner") + 1], c[c.index("--label") + 1]) for c in gh["calls"] if c[0] == "search"]
    assert searches == [("example-org", "incident"), ("example-org", "design-review")]


def test_command_prints_markdown_and_json(tsite, tmp_path, gh, capsys):  # pylint: disable=unused-argument
    pir(tmp_path, "2026-11-01-a", services=["example-service"])
    args = ["--site", str(tsite), "--as-of", "2026-11-30", "--pirs", str(tmp_path)]
    assert trends.main(args) == 0
    assert capsys.readouterr().out.startswith("## Recommendations backlog")
    assert trends.main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pirs_in_window"] == 1
    assert trends.main([*args[:2], "--as-of", "soon"]) == 2
