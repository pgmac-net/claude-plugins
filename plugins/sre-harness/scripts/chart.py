#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6", "matplotlib>=3.8"]
# ///
"""Draw a graph as a PNG, read-only, for evidence and for share.py.

Commands:
  prom    a PromQL range query against the site's metrics backend (slo.url, the
          same read token and no-redirect rule as slo.py), drawn as a line chart
  nagios  the nagiosgraph graph of one host and service, fetched through the
          status page's signed graph proxy (monitoring[type=nagios].graph_proxy)

Both only read. The output must be a .png under the site's evidence directory
(incident.evidence_dir) or the system temp directory, so the call cannot write
anywhere else. Credentials come from files in ~/.config/sre-harness, which the
chat policy denies to the agent's own reads; they are never printed.

Usage:
  chart.py prom '<promql>' --since 6h [--until now] [--step 60] --title "..." -o <file.png>
  chart.py nagios <host> '<service>' [--period day] [--at now] -o <file.png>

Prints one JSON line with the file written. Exit codes: 0 drawn, 2 refused or
failed, 3 no data (an empty query, or nagiosgraph's "No data available").
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import share
import slo
import sre_common
from sre_common import SiteError

MAX_RANGE_SECONDS = 7 * 86400
MAX_POINTS = 1500
MIN_STEP = 15
MAX_SERIES = 20
PERIODS = ("day", "week", "month", "quarter", "year")
GRAPH_TTL = 300
FETCH_TIMEOUT = 20
# Cloudflare in front of the status page answers 403 to Python's default 'Python-urllib/x.y'.
USER_AGENT = "sre-harness-chart/1"
MAX_GRAPH_BYTES = 2 * 1024 * 1024
EMPTY_GRAPH_HEIGHT = 50  # nagiosgraph's "No data available" PNG is 23 pixels tall
DURATION = re.compile(r"^(\d{1,4})([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_KEY_FILE = 4096
NO_DATA = 3


class NoData(SiteError):
    """The source answered, with nothing to draw."""


# --- where output may go -------------------------------------------------------------


def allowed_roots(doc: dict[str, Any]) -> list[Path]:
    """The directories a chart may be written under: the evidence directory and the temp directory."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    evidence = (doc.get("incident") or {}).get("evidence_dir")
    if evidence:
        roots.append(Path(evidence).expanduser().resolve())
    return roots


def output_path(raw: str, doc: dict[str, Any]) -> Path:
    """The checked output path. Its parent directories are made if they sit under an allowed root."""
    path = Path(raw).expanduser()
    if path.suffix.lower() != ".png":
        raise SiteError("the output must be a .png file")
    resolved = path.resolve()
    roots = allowed_roots(doc)
    if not any(root in resolved.parents for root in roots):
        shown = ", ".join(str(root) for root in roots)
        raise SiteError(f"charts are written only under {shown}; {resolved} is not")
    if share.denied(path):
        raise SiteError(f"{path} is in a location the chat policy denies")
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_file()):
        raise SiteError(f"{resolved} exists and is not a regular file")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


# --- time ---------------------------------------------------------------------------------


def moment(text: str, now: float) -> float:
    """'now', a Unix time, an ISO time (no offset = UTC) or a duration ago ('2h')."""
    if text == "now":
        return now
    if text.isdigit():
        return float(text)
    ago = DURATION.match(text)
    if ago:
        return now - int(ago.group(1)) * UNITS[ago.group(2)]
    try:
        return sre_common.parse_utc(text).timestamp()
    except ValueError as exc:
        raise SiteError(f"cannot read the time '{text}': use now, a Unix time, ISO 8601 or a duration like 2h") from exc


def span(since: str, until: str, step: int | None, now: float | None = None) -> tuple[float, float, int]:
    """(start, end, step) of a range query, within the caps."""
    now = time.time() if now is None else now
    match = DURATION.match(since)
    end = min(moment(until, now), now)
    start = end - int(match.group(1)) * UNITS[match.group(2)] if match else moment(since, now)
    if start >= end:
        raise SiteError("the range is empty: --since must be before --until")
    if end - start > MAX_RANGE_SECONDS:
        raise SiteError(f"the range is longer than {MAX_RANGE_SECONDS // 86400} days")
    chosen = max(MIN_STEP, step or math.ceil((end - start) / MAX_POINTS))
    if (end - start) / chosen > MAX_POINTS * 4:
        raise SiteError(f"--step {chosen} gives too many points; use a larger step")
    return start, end, chosen


# --- prometheus ---------------------------------------------------------------------------


def range_query(settings: dict[str, Any], promql: str, token: str, window: tuple[float, float, int]) -> list[dict]:
    """A range query against the site's metrics backend, as slo.query does an instant one."""
    start, end, step = window
    params = {"query": promql, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": str(step)}
    return slo.api_get(settings, "/api/v1/query_range", params, token)


def label(metric: dict[str, str]) -> str:
    """A short legend entry: the labels that tell series apart, without the metric name."""
    shown = {key: value for key, value in metric.items() if key != "__name__"}
    if not shown:
        return metric.get("__name__", "value")
    return ", ".join(f"{key}={value}" for key, value in sorted(shown.items()))[:80]


def draw_series(series: list[dict], title: str, out: Path, timezone: str) -> None:
    """A line chart of range-query results, times in the site's zone."""
    import matplotlib  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    matplotlib.use("Agg")
    from zoneinfo import ZoneInfo  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    import matplotlib.dates as mdates  # noqa: PLC0415  pylint: disable=import-outside-toplevel
    import matplotlib.pyplot as plt  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    zone = ZoneInfo(timezone)
    figure, axes = plt.subplots(figsize=(10, 4.5), dpi=110)
    for item in series[:MAX_SERIES]:
        points = [(datetime.fromtimestamp(float(t), UTC), float(v)) for t, v in item.get("values", [])]
        points = [(t, v) for t, v in points if math.isfinite(v)]
        if points:
            axes.plot([t for t, _ in points], [v for _, v in points], linewidth=1.2, label=label(item["metric"]))
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M", tz=zone))
    axes.set_xlabel(f"time ({timezone})")
    axes.grid(True, alpha=0.3)
    axes.set_title(title, fontsize=11)
    if 1 < len(series) <= MAX_SERIES:
        axes.legend(fontsize=7, loc="upper left", framealpha=0.8)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(out, format="png")
    plt.close(figure)


def cmd_prom(args: argparse.Namespace, doc: dict[str, Any]) -> dict[str, Any]:
    settings = doc.get("slo") or {}
    if not settings.get("url"):
        raise SiteError("the site has no metrics backend (slo.url in site.yaml)")
    out = output_path(args.output, doc)
    window = span(args.since, args.until, args.step)
    series = range_query(settings, args.query, slo.read_token(settings), window)
    if not series or not any(item.get("values") for item in series):
        raise NoData("the query returned no data in that range")
    timezone = (doc.get("incident") or {}).get("timezone") or "UTC"
    title = share.clean_title(args.title or args.query)
    draw_series(series, title, out, timezone)
    return {"file": str(out), "series": len(series), "drawn": min(len(series), MAX_SERIES), "step": window[2]}


# --- nagiosgraph ----------------------------------------------------------------------------


def graph_proxy(doc: dict[str, Any]) -> dict[str, str]:
    """The nagios monitoring entry's graph_proxy settings."""
    for source in doc.get("monitoring", []):
        if source.get("type") == "nagios" and source.get("graph_proxy"):
            return source["graph_proxy"]
    raise SiteError("no nagios monitoring entry has a graph_proxy in site.yaml")


def signing_key(key_file: str) -> bytes:
    """The proxy's signing secret, from a file the agent cannot read: owner-only, regular, small."""
    path = slo.token_dir() / key_file
    try:
        info = path.lstat()
    except OSError as exc:
        raise SiteError(f"no graph signing key file at {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SiteError(f"{path} must be a regular file, not a link or anything else")
    if info.st_mode & 0o077:
        raise SiteError(f"{path} must be readable by its owner only (chmod 600)")
    if info.st_size > MAX_KEY_FILE:
        raise SiteError(f"{path} is too large to be a key")
    key = path.read_text(encoding="utf-8").strip()
    if not key or "\n" in key:
        raise SiteError(f"{path} must hold the key on a single line")
    return key.encode()


def signed_url(proxy: dict[str, str], key: bytes, fields: tuple[str, str, str, int], now: int) -> str:
    """The proxy URL: HMAC-SHA256 over 'host|service|period|timet|expires' (nagios-public-status-page)."""
    host, service, period, timet = fields
    expires = now + GRAPH_TTL
    payload = f"{host}|{service}|{period}|{timet}|{expires}".encode()
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    query = urllib.parse.urlencode(
        {"host": host, "service": service, "period": period, "timet": timet, "expires": expires, "sig": sig},
        quote_via=urllib.parse.quote,
    )
    return f"{proxy['url'].rstrip('/')}/api/graph?{query}"


def png_height(data: bytes) -> int:
    """A PNG's height from its IHDR chunk; 0 if it is not a PNG."""
    if share.sniff(data[:16]) != "png" or len(data) < 24:
        return 0
    return int.from_bytes(data[20:24], "big")


def fetch_graph(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https, site config
    try:
        with slo._OPENER.open(request, timeout=FETCH_TIMEOUT) as response:  # pylint: disable=protected-access
            data = response.read(MAX_GRAPH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SiteError(f"the graph proxy answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SiteError(f"the graph proxy could not be reached: {type(exc).__name__}") from exc
    if len(data) > MAX_GRAPH_BYTES:
        raise SiteError("the graph proxy sent more than a graph")
    return data


def cmd_nagios(args: argparse.Namespace, doc: dict[str, Any]) -> dict[str, Any]:
    if not NAME.fullmatch(args.host):
        raise SiteError(f"'{args.host}' is not a host name")
    if not args.service.strip() or len(args.service) > 128 or not args.service.isprintable():
        raise SiteError("the service name must be printable text of at most 128 characters")
    proxy = graph_proxy(doc)
    out = output_path(args.output, doc)
    now = int(time.time())
    timet = int(min(moment(args.at, now), now))
    url = signed_url(proxy, signing_key(proxy["key_file"]), (args.host, args.service, args.period, timet), now)
    data = fetch_graph(url)
    height = png_height(data)
    if height == 0:
        raise SiteError("the graph proxy did not send a PNG")
    if height < EMPTY_GRAPH_HEIGHT:
        raise NoData(f"no graph data for {args.host}/{args.service}: check the service name, case and spaces exact")
    out.write_bytes(data)
    return {"file": str(out), "host": args.host, "service": args.service, "period": args.period, "timet": timet}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draw a read-only graph as a PNG.", allow_abbrev=False)
    parser.add_argument("--site", help="site repository (default: SRE_HARNESS_SITE or the pointer file)")
    sub = parser.add_subparsers(dest="command", required=True)
    prom = sub.add_parser("prom", help="a PromQL range query as a line chart", allow_abbrev=False)
    prom.add_argument("query", help="PromQL")
    prom.add_argument("--since", default="6h", help="start: a duration before --until (6h), or a time")
    prom.add_argument("--until", default="now", help="end: now, a Unix time, ISO 8601, or a duration ago")
    prom.add_argument("--step", type=int, help="seconds between points (default: fits the range)")
    prom.add_argument("--title", help="chart title (default: the query)")
    prom.add_argument("-o", "--output", required=True, help=".png under the evidence or temp directory")
    prom.set_defaults(func=cmd_prom)
    nagios = sub.add_parser("nagios", help="the nagiosgraph graph of one service", allow_abbrev=False)
    nagios.add_argument("host")
    nagios.add_argument("service", help="exact service name, as Nagios shows it")
    nagios.add_argument("--period", choices=PERIODS, default="day")
    nagios.add_argument("--at", default="now", help="window end: now, a Unix time, ISO 8601, or a duration ago")
    nagios.add_argument("-o", "--output", required=True, help=".png under the evidence or temp directory")
    nagios.set_defaults(func=cmd_nagios)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _root, doc = sre_common.load_site(args.site)
        print(json.dumps(args.func(args, doc), sort_keys=True))
    except NoData as exc:
        print(f"NO DATA {exc}", file=sys.stderr)
        return NO_DATA
    except SiteError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
