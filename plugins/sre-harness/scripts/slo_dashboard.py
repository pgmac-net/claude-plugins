"""A Grafana dashboard for every SLO, generated from the same definitions as the recording rules.

The dashboard reads only the recorded slo:* series and, for a probe SLI, the raw probe
series. It never restates the budget maths: those live in the rules, so the dashboard,
slo.py status and the agent always agree. Output is deterministic, so a stale file in
the site repository can be detected in CI.
"""

from __future__ import annotations

import json
from typing import Any

DASHBOARD_UID = "sre-harness-slos"
DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
ROW_HEIGHT = 8
HEADER_HEIGHT = 3
# The plugin's public icon. A plain markdown image, at the size of the file, so Grafana's HTML
# sanitiser has no size attribute to drop.
ICON_URL = "https://raw.githubusercontent.com/pgmac-net/claude-plugins/main/plugins/sre-harness/assets/icon-64.png"


def _seconds(duration: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(duration[:-1]) * units[duration[-1]]


def _stat(title: str, expr: str, grid: dict[str, int], look: dict[str, Any]) -> dict:
    """A single-number panel. look: unit, decimals and threshold steps."""
    return {
        "type": "stat",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": grid,
        "targets": [{"refId": "A", "expr": expr, "instant": True, "datasource": DATASOURCE}],
        "fieldConfig": {
            "defaults": {
                "unit": look["unit"],
                "decimals": look["decimals"],
                "noValue": "no data",
                "thresholds": {"mode": "absolute", "steps": look["steps"]},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def _timeseries(title: str, targets: list[tuple[str, str]], unit: str, grid: dict[str, int], max_value: float) -> dict:
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": grid,
        "targets": [
            {"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": DATASOURCE}
            for i, (legend, expr) in enumerate(targets)
        ],
        "fieldConfig": {
            "defaults": {"unit": unit, "min": 0, "max": max_value, "custom": {"lineWidth": 1, "fillOpacity": 10}},
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }


def _panels_for(slo: Any, y: int, ids: list[int]) -> list[dict]:
    """A row for one SLO: headline numbers on the left, history on the right."""
    sel = f'{{slo="{slo.name}"}}'
    allowed = 1 - slo.target
    budget_minutes = allowed * _seconds(slo.window) / 60
    budget_word = "spends budget" if slo.budget else "informational, not budgeted"
    panels: list[dict] = [
        {
            "type": "row",
            "title": f"{slo.name}: {slo.target:.2%} over {slo.window} ({budget_word})",
            "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "panels": [],
        }
    ]
    y += 1
    attainment_steps = [
        {"color": "red", "value": None},
        {"color": "green", "value": slo.target},
    ]
    panels.append(
        _stat(
            f"Attainment ({slo.window})",
            f"1 - slo:sli_error:ratio_rate{slo.window}{sel}",
            {"h": ROW_HEIGHT // 2, "w": 4, "x": 0, "y": y},
            {"unit": "percentunit", "decimals": 3, "steps": attainment_steps},
        )
    )
    if slo.budget:
        panels.append(
            _stat(
                "Budget left (minutes)",
                f"slo:error_budget:remaining_ratio{sel} * {budget_minutes:g}",
                {"h": ROW_HEIGHT // 2, "w": 4, "x": 4, "y": y},
                {
                    "unit": "m",
                    "decimals": 1,
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "orange", "value": 0},
                        {"color": "green", "value": budget_minutes * 0.25},
                    ],
                },
            )
        )
        burn_steps = [{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 6}]
        for column, window in enumerate(("1h", "6h")):
            panels.append(
                _stat(
                    f"Burn rate ({window})",
                    f"slo:sli_error:ratio_rate{window}{sel} / {allowed:.6g}",
                    {"h": ROW_HEIGHT // 2, "w": 4, "x": 4 * column, "y": y + ROW_HEIGHT // 2},
                    {"unit": "x", "decimals": 2, "steps": burn_steps},
                )
            )
    history = [
        ("error ratio, 5m", f"slo:sli_error:ratio_rate5m{sel}"),
        ("error ratio, 1h", f"slo:sli_error:ratio_rate1h{sel}"),
        (f"error ratio, {slo.window}", f"slo:sli_error:ratio_rate{slo.window}{sel}"),
    ]
    panels.append(_timeseries("Error ratio", history, "percentunit", {"h": ROW_HEIGHT, "w": 8, "x": 8, "y": y}, 1))
    if slo.counter:
        source = [("good / total, 5m", f"sum(rate({slo.good}[5m])) / sum(rate({slo.total}[5m]))")]
        title = "Good events share"
    else:
        source = [("probe success", slo.good)]
        title = "Probe results"
    panels.append(_timeseries(title, source, "percentunit", {"h": ROW_HEIGHT, "w": 8, "x": 16, "y": y}, 1))
    for panel in panels:
        ids.append(len(ids) + 1)
        panel["id"] = ids[-1]
    return panels


def _header() -> dict:
    """The icon and one line of purpose, above every row."""
    return {
        "type": "text",
        "title": "",
        "transparent": True,
        "gridPos": {"h": HEADER_HEIGHT, "w": 24, "x": 0, "y": 0},
        "options": {
            "mode": "markdown",
            "content": f"![SRE harness]({ICON_URL}) &nbsp; **SRE harness**: service level objectives for the services "
            "that have one, read from the site's journeys.",
        },
    }


def build(slos: list[Any], title: str = "SRE harness: SLOs") -> dict[str, Any]:
    """The dashboard model: an icon header, then one row per SLO, budget-spending SLOs first."""
    ordered = sorted(slos, key=lambda s: (not s.budget, s.service, s.name))
    header = _header()
    header["id"] = 1
    panels: list[dict] = [header]
    ids: list[int] = [1]
    y = HEADER_HEIGHT
    for slo in ordered:
        panels.extend(_panels_for(slo, y, ids))
        y += 1 + ROW_HEIGHT
    return {
        "uid": DASHBOARD_UID,
        "title": title,
        "description": (
            "Generated by sre-harness slo.py dashboard from the site's OpenSLO files. Do not edit in Grafana."
        ),
        "tags": ["sre-harness", "slo", "generated"],
        "editable": False,
        "schemaVersion": 39,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "timezone": "browser",
        "templating": {
            "list": [
                {
                    "type": "datasource",
                    "name": "datasource",
                    "label": "Metrics",
                    "query": "prometheus",
                    "hide": 0,
                }
            ]
        },
        "annotations": {"list": []},
        "panels": panels,
    }


def render(slos: list[Any]) -> str:
    return json.dumps(build(slos), indent=2) + "\n"
