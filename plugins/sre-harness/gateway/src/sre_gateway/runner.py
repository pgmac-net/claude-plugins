"""The alert source: what the gateway polls, and what it remembers between polls.

The rules for which problems start a triage live in the plugin's alerts.py, so
they can be run and tested without the gateway. This module adds the polling
side: fetching from the source off the event loop, the catalog index, and the
record of problems already triaged and services cooling down, kept across
restarts.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol

from sre_gateway import harness
from sre_gateway.store import write_private

Alert = harness.alerts.Alert
Selection = harness.alerts.Selection
SEEN_DAYS = 7


class AlertFeed(Protocol):
    async def problems(self) -> list[Alert]:
        """Every unhandled HARD problem the source reports now."""

    def index(self) -> dict[str, list[tuple[str, int | None]]]:
        """Monitored host -> [(service ref, tier)] from the catalog."""


class NagiosFeed:
    """statusjson.cgi, read with the site's runner settings and the credentials from the environment."""

    def __init__(self, site_root: Path, doc: dict[str, Any], auth: tuple[str, str] | None):
        self._site_root = site_root
        self._doc = doc
        self._url = harness.alerts.status_url(doc)
        self._auth = auth
        self._index: dict[str, list[tuple[str, int | None]]] | None = None

    async def problems(self) -> list[Alert]:
        hosts = await asyncio.to_thread(harness.alerts.fetch, self._url, "hostlist", self._auth)
        services = await asyncio.to_thread(harness.alerts.fetch, self._url, "servicelist", self._auth)
        return harness.alerts.parse(hosts, services)

    def index(self) -> dict[str, list[tuple[str, int | None]]]:
        # Read once: the catalog changes by pull request, and the gateway restarts to pick up site changes.
        if self._index is None:
            self._index = harness.alerts.host_services(self._site_root, self._doc)
        return self._index


@dataclass
class RunnerSettings:
    enabled: bool
    tiers: list[int]
    poll_seconds: int
    max_auto_sessions: int
    cooldown_minutes: int
    min_state_age_seconds: int

    @classmethod
    def from_site(cls, doc: dict[str, Any]) -> RunnerSettings:
        values = harness.alerts.settings(doc)
        return cls(**{item.name: values[item.name] for item in fields(cls)})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunnerState:
    """Problems already triaged (by key) and when each service's last triage started, kept on disk."""

    path: Path
    seen: dict[str, float] = field(default_factory=dict)
    cooldowns: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> RunnerState:
        state = cls(path)
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            state.seen = {str(k): float(v) for k, v in (raw.get("seen") or {}).items()}
            state.cooldowns = {str(k): float(v) for k, v in (raw.get("cooldowns") or {}).items()}
        return state

    def mark(self, alerts: list[Alert], now: float) -> None:
        for alert in alerts:
            self.seen[alert.key] = now
            for ref in alert.services:
                self.cooldowns[ref] = now
        horizon = now - SEEN_DAYS * 86400
        self.seen = {key: when for key, when in self.seen.items() if when >= horizon}
        self.save()

    def save(self) -> None:
        data = {"v": 1, "seen": self.seen, "cooldowns": self.cooldowns}
        write_private(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")


class Runner:
    """Decides, each poll, which problems start a triage. Holds no chat or agent logic."""

    def __init__(self, settings: RunnerSettings, feed: AlertFeed, state: RunnerState, clock=time.time):
        self.settings = settings
        self.feed = feed
        self.state = state
        self._clock = clock

    async def poll(self, starting: bool = True) -> Selection:
        """New qualifying problems. The cooldown only stops a new triage; with starting=False (an incident is
        open, so they are relayed into it) it does not apply."""
        alerts = await self.feed.problems()
        cooldowns = self.state.cooldowns if starting else {}
        return harness.alerts.select(
            alerts,
            self.feed.index(),
            self.settings.as_dict(),
            seen=set(self.state.seen),
            cooldowns=cooldowns,
            now=self._clock(),
        )

    def drill(self, target: str, text: str) -> Alert:
        """A made-up problem for a drill: any tier, no cooldown, never recorded as seen."""
        host, _, check = target.partition("/")
        services = tuple(sorted({ref for ref, _tier in self.feed.index().get(host, [])}))
        return Alert(host, check or None, "CRITICAL", text or "drill: simulated alert", int(self._clock()), services)

    def mark(self, alerts: list[Alert]) -> None:
        self.state.mark(alerts, self._clock())
