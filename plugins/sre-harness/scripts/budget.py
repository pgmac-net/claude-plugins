"""Error-budget enforcement and history: the Tier 2 gate, the Nagios check and daily snapshots.

Everything here reads budget state the same way `slo.py status` does, from the
recorded series, so the alert, the gate and the report can never disagree.

- The gate answers, at the moment a command would be Tier 2, whether an error
  budget forbids it. It fails closed: an exhausted budget, a budget whose
  state is unknown, or a backend that cannot be read all keep the action at
  Tier 1. A service no budgeted SLO governs is never affected.
- The Nagios check turns budget states into one line, an exit code and
  performance data.
- A snapshot is one JSON line per SLO per day, written to a history
  directory. Each run also fills any missing day still inside the backend's
  retention, so a missed run repairs itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import slo
from sre_common import SiteError, iso_utc, utc_now

# --- the Tier 2 gate -----------------------------------------------------------


class BudgetGate:
    """Whether an error budget keeps a service's pre-approved actions at Tier 1.

    Nothing is read until the first question, and the backend is queried at
    most once, so a command that is Tier 1 anyway costs nothing.
    """

    def __init__(
        self,
        site_root: Path,
        doc: dict[str, Any],
        token_reader: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.site_root = site_root
        self.doc = doc
        self.token_reader = token_reader or slo.read_token
        self._loaded = False
        self._invalid: str | None = None
        self._scopes: list[tuple[slo.Slo, list[str]]] = []
        self._states: dict[str, dict[str, Any]] | None = None
        self._read_error: str | None = None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.doc.get("slo"):
            return
        found = slo.load(self.site_root, self.doc)
        if found.errors:
            self._invalid = (
                f"the site's journeys or SLOs are not valid ({len(found.errors)} problem(s)), "
                "so no error budget can be trusted; run slo.py check"
            )
            return
        self._scopes = [(one, slo.freeze_scope(one, found.journeys)) for one in found.slos if one.budget]

    def governed(self) -> set[str]:
        """Services some budgeted SLO can freeze."""
        self._load()
        return {ref for _one, scope in self._scopes for ref in scope}

    def _read_states(self) -> None:
        if self._states is not None or self._read_error is not None:
            return
        settings = self.doc["slo"]
        try:
            results = slo.status(settings, [one for one, _scope in self._scopes], self.token_reader(settings))
        except SiteError as exc:
            self._read_error = str(exc)
            return
        self._states = {result["slo"]: result for result in results}

    def reason(self, service: str) -> str | None:
        """Why the error budget keeps this service at Tier 1, or None if it does not."""
        self._load()
        if self._invalid:
            return self._invalid
        governing = [one for one, scope in self._scopes if service in scope]
        if not governing:
            return None
        self._read_states()
        if self._read_error is not None:
            return (
                f"the error budget governing {service} could not be read ({self._read_error}); "
                "Tier 2 stays off until it can"
            )
        for one in governing:
            result = (self._states or {}).get(one.name, {"state": "unknown"})
            if result["state"] == "exhausted":
                return f"the error budget of SLO {one.name} is exhausted; {service} is frozen and Tier 2 is off for it"
            if result["state"] == "unknown":
                why = result.get("reason", "no data")
                return (
                    f"the error budget of SLO {one.name} is in an unknown state ({why}); Tier 2 stays off for {service}"
                )
        return None


# --- the Nagios check ------------------------------------------------------------

NAGIOS_CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}
# Worst first. An unknown budget turns Tier 2 off, so it outranks burning fast.
NAGIOS_ORDER = ("CRITICAL", "UNKNOWN", "WARNING", "OK")
STATE_LEVEL = {
    "exhausted": "CRITICAL",
    "unknown": "UNKNOWN",
    "burning fast": "WARNING",
    "healthy": "OK",
    "informational": "OK",
}


def ascii_text(text: str) -> str:
    """Plugin output Nagios can carry: ASCII only, and no '|', which would start performance data."""
    return "".join(ch if 32 <= ord(ch) < 127 and ch != "|" else "?" for ch in text)


def _describe(result: dict[str, Any]) -> str:
    name, state = result["slo"], result["state"]
    if state == "unknown":
        return f"{name} unknown: {result.get('reason', 'no data')}"
    burn = result.get("burn_rate", {})
    burns = " ".join(f"{w} {burn[w]:.2f}" for w in slo.FAST_BURN if w in burn)
    if state == "informational":
        return f"{name} attainment {result['attainment'] * 100:.3f}% (not budgeted)"
    text = (
        f"{name} {state}: budget {result['budget_remaining'] * 100:.1f}% "
        f"({result['budget_remaining_minutes']:.1f} min), burn {burns or 'n/a'}"
    )
    if result.get("frozen"):
        text += f"; FREEZE {', '.join(ref.removeprefix('component:') for ref in result['freeze_scope'])}"
    return text


def _perfdata(result: dict[str, Any]) -> list[str]:
    if result["state"] in ("unknown", "informational"):
        return []
    name = result["slo"]
    out = [f"{name}_budget={result['budget_remaining']:.4f};;0:"]
    for window, limit in slo.FAST_BURN.items():
        if window in result.get("burn_rate", {}):
            out.append(f"{name}_burn_{window}={result['burn_rate'][window]:.2f};{limit:g};;0;")
    return out


def nagios(results: list[dict[str, Any]]) -> tuple[int, str]:
    """Exit code and output line for a set of SLO states."""
    if not results:
        return NAGIOS_CODES["UNKNOWN"], "SLO UNKNOWN - no SLO matched"
    levels = {STATE_LEVEL.get(result["state"], "UNKNOWN") for result in results}
    level = next(name for name in NAGIOS_ORDER if name in levels)
    text = "; ".join(_describe(result) for result in results)
    perf = " ".join(item for result in results for item in _perfdata(result))
    line = ascii_text(f"SLO {level} - {text}")
    return NAGIOS_CODES[level], f"{line} | {perf}" if perf else line


# --- daily snapshots -------------------------------------------------------------


def day_end(day: date, timezone: str) -> datetime:
    """The instant a local day ends: the next local midnight."""
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(timezone))


def _by_slo(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        try:
            out[row["metric"]["slo"]] = float(row["value"][1])
        except (KeyError, ValueError, IndexError):
            continue
    return out


def _day_values(
    settings: dict[str, Any], names: list[str], token: str, start: datetime, end: datetime
) -> dict[str, dict[str, float]]:
    """Recorded values for one local day: at its end, and the worst during it."""
    match = "|".join(names)
    span = f"{int((end - start).total_seconds())}s"
    at = end.timestamp()
    queries = {
        "error_1d": f'slo:sli_error:ratio_rate1d{{slo=~"{match}"}}',
        "remaining": f'slo:error_budget:remaining_ratio{{slo=~"{match}"}}',
        "remaining_min": f'min_over_time(slo:error_budget:remaining_ratio{{slo=~"{match}"}}[{span}])',
        **{
            f"burn_max_{w}": f'max_over_time(slo:sli_error:ratio_rate{w}{{slo=~"{match}"}}[{span}])'
            for w in slo.FAST_BURN
        },
    }
    values: dict[str, dict[str, float]] = {}
    for key, promql in queries.items():
        for name, value in _by_slo(slo.query(settings, promql, token, at=at)).items():
            values.setdefault(name, {})[key] = value
    return values


def day_state(one: slo.Slo, values: dict[str, float]) -> str:
    """The worst budget state an SLO reached during a day."""
    if not one.budget:
        return "informational"
    if "remaining_min" not in values:
        return "unknown"
    if values["remaining_min"] <= 0:
        return "exhausted"
    allowed = 1 - one.target
    if any(values.get(f"burn_max_{w}", 0) / allowed > limit for w, limit in slo.FAST_BURN.items()):
        return "burning fast"
    return "healthy"


def snapshot_row(one: slo.Slo, scope: list[str], day: date, values: dict[str, float], on_time: bool) -> dict[str, Any]:
    """One SLO's line for one day."""
    allowed = 1 - one.target
    error = values["error_1d"]
    row: dict[str, Any] = {
        "date": day.isoformat(),
        "slo": one.name,
        "service": f"component:{one.service}",
        "journey": one.journey,
        "target": one.target,
        "window": one.window,
        "spends_budget": one.budget,
        "error_ratio_1d": round(error, 6),
        "attainment_1d": round(1 - error, 6),
        "budget_remaining": round(values["remaining"], 4) if "remaining" in values else None,
        "budget_remaining_min": round(values["remaining_min"], 4) if "remaining_min" in values else None,
        "burn_max": {
            w: round(values[f"burn_max_{w}"] / allowed, 2) for w in slo.FAST_BURN if f"burn_max_{w}" in values
        },
        "state": day_state(one, values),
        "captured": "on time" if on_time else "backfilled",
        "recorded_at": iso_utc(utc_now()),
    }
    if one.budget:
        row["freeze_scope"] = scope
        row["frozen"] = row["state"] == "exhausted"
    return row


def month_file(history: Path, day: date) -> Path:
    return history / f"{day:%Y-%m}.jsonl"


def read_history(history: Path) -> list[dict[str, Any]]:
    """Every snapshot line in a history directory. Unreadable lines are skipped."""
    return [row for path in sorted(history.glob("*.jsonl")) for row in read_history_file(path)]


def _write_month(path: Path, rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda row: (row["date"], row["slo"]))
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def snapshot(  # noqa: PLR0913  pylint: disable=too-many-arguments,too-many-locals
    history: Path,
    settings: dict[str, Any],
    found: slo.Findings,
    token: str,
    *,
    timezone: str,
    today: date | None = None,
) -> dict[str, Any]:
    """Write every missing day still inside retention, up to yesterday. Lines already written are kept."""
    today = today or datetime.now(ZoneInfo(timezone)).date()
    yesterday = today - timedelta(days=1)
    have = {(row["date"], row["slo"]) for row in read_history(history)}
    scopes = {one.name: slo.freeze_scope(one, found.journeys) for one in found.slos}
    written, no_data = 0, []
    for back in range(settings["retention_days"] - 1, 0, -1):
        day = today - timedelta(days=back)
        wanted = [one for one in found.slos if (day.isoformat(), one.name) not in have]
        if not wanted:
            continue
        start, end = day_end(day - timedelta(days=1), timezone), day_end(day, timezone)
        values = _day_values(settings, [one.name for one in wanted], token, start, end)
        rows = []
        for one in wanted:
            if "error_1d" not in values.get(one.name, {}):
                no_data.append(f"{day.isoformat()} {one.name}")
                continue
            rows.append(snapshot_row(one, scopes[one.name], day, values[one.name], on_time=day == yesterday))
        if rows:
            path = month_file(history, day)
            _write_month(path, read_history_file(path) + rows)
            written += len(rows)
    return {"written": written, "no_data": no_data, "through": yesterday.isoformat()}


def read_history_file(path: Path) -> list[dict[str, Any]]:
    """The snapshot lines of one month file, or none."""
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "date" in row and "slo" in row:
            rows.append(row)
    return rows
