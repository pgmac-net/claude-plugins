"""Tests for chart.py: where a chart may be written, the range caps, and the signed nagiosgraph fetch."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import struct
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import chart
import pytest
import share
from sre_common import SiteError

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "sre-harness").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return home


def doc_with(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    return {"incident": {"evidence_dir": str(tmp_path / "evidence"), "timezone": "Etc/UTC"}, **extra}


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x00" * 16


# --- where output may go -----------------------------------------------------------------


def test_a_chart_may_go_under_the_evidence_directory(tmp_path: Path):
    out = chart.output_path(str(tmp_path / "evidence" / "inc-1" / "load.png"), doc_with(tmp_path))
    assert out.parent.is_dir()


def test_a_chart_may_go_under_the_temp_directory(tmp_path: Path):
    assert chart.output_path(str(tmp_path / "tmp" / "x" / "load.png"), doc_with(tmp_path))


@pytest.mark.parametrize("where", ["site/load.png", "home/.bashrc.png", "elsewhere/x.png"])
def test_anywhere_else_is_refused(tmp_path: Path, where: str):
    with pytest.raises(SiteError, match="only under"):
        chart.output_path(str(tmp_path / where), doc_with(tmp_path))


def test_only_png_is_written(tmp_path: Path):
    with pytest.raises(SiteError, match=r"\.png"):
        chart.output_path(str(tmp_path / "tmp" / "x.sh"), doc_with(tmp_path))


def test_a_dot_dot_path_is_judged_after_resolving(tmp_path: Path):
    with pytest.raises(SiteError, match="only under"):
        chart.output_path(str(tmp_path / "tmp" / ".." / "site" / "x.png"), doc_with(tmp_path))


def test_an_existing_link_is_not_written_through(tmp_path: Path):
    target = tmp_path / "tmp" / "real.png"
    target.write_bytes(b"x")
    link = tmp_path / "tmp" / "link.png"
    link.symlink_to(tmp_path / "home" / "victim.png")
    with pytest.raises(SiteError, match="only under"):
        chart.output_path(str(link), doc_with(tmp_path))


# --- the range ---------------------------------------------------------------------------


def test_a_duration_since_is_counted_back_from_until():
    start, end, step = chart.span("6h", "now", None, now=NOW)
    assert (end - start, end) == (6 * 3600, NOW)
    assert step >= chart.MIN_STEP


def test_until_in_the_future_is_clamped_to_now():
    _, end, _ = chart.span("1h", str(int(NOW + 3600)), None, now=NOW)
    assert end == NOW


def test_a_range_over_seven_days_is_refused():
    with pytest.raises(SiteError, match="longer than 7 days"):
        chart.span("8d", "now", None, now=NOW)


def test_a_backwards_range_is_refused():
    with pytest.raises(SiteError, match="empty"):
        chart.span("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", None, now=NOW)


def test_a_tiny_step_is_refused_when_it_gives_too_many_points():
    with pytest.raises(SiteError, match="too many points"):
        chart.span("7d", "now", 15, now=NOW)


def test_a_time_that_cannot_be_read_is_refused():
    with pytest.raises(SiteError, match="cannot read"):
        chart.moment("yesterday-ish", NOW)


# --- prometheus --------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_prom_queries_the_range_and_draws_a_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("matplotlib")
    seen: dict[str, Any] = {}
    body = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"instance": "k8s01"}, "values": [[NOW - 60, "1"], [NOW, "2"]]},
                {"metric": {"instance": "k8s02"}, "values": [[NOW - 60, "3"], [NOW, "NaN"]]},
            ]
        },
    }

    def fake_open(request, timeout):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        seen["auth"] = request.unredirected_hdrs.get("Authorization")
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(chart.slo._OPENER, "open", fake_open)  # pylint: disable=protected-access
    monkeypatch.setenv("METRICS_TOKEN", "tok_abcdefgh")
    doc = doc_with(
        tmp_path, slo={"url": "https://prom.example.com/api/prom", "user": "42", "token_env": "METRICS_TOKEN"}
    )
    out = tmp_path / "tmp" / "load.png"
    args = argparse.Namespace(query="node_load1", since="1h", until="now", step=None, title="load", output=str(out))
    result = chart.cmd_prom(args, doc)
    assert result["series"] == 2
    assert "/api/v1/query_range?" in seen["url"]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)["query"] == ["node_load1"]
    assert seen["auth"].startswith("Basic ")
    assert share.sniff(out.read_bytes()[:16]) == "png"


def test_prom_with_no_data_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = {"status": "success", "data": {"result": []}}
    monkeypatch.setattr(chart.slo._OPENER, "open", lambda r, timeout: FakeResponse(json.dumps(body).encode()))  # pylint: disable=protected-access
    monkeypatch.setenv("METRICS_TOKEN", "tok_abcdefgh")
    doc = doc_with(tmp_path, slo={"url": "https://prom.example.com", "token_env": "METRICS_TOKEN"})
    args = argparse.Namespace(
        query="up", since="1h", until="now", step=None, title=None, output=str(tmp_path / "tmp" / "u.png")
    )
    with pytest.raises(chart.NoData):
        chart.cmd_prom(args, doc)


def test_prom_needs_a_metrics_backend(tmp_path: Path):
    args = argparse.Namespace(
        query="up", since="1h", until="now", step=None, title=None, output=str(tmp_path / "tmp" / "u.png")
    )
    with pytest.raises(SiteError, match="no metrics backend"):
        chart.cmd_prom(args, doc_with(tmp_path))


# --- nagiosgraph -------------------------------------------------------------------------

PROXY = {"url": "https://status.example.com", "key_file": "nagiosgraph-key"}


def nagios_doc(tmp_path: Path) -> dict[str, Any]:
    return doc_with(tmp_path, monitoring=[{"name": "alerts", "type": "nagios", "graph_proxy": PROXY}])


def write_key(home: Path, text: str = "s3cret-signing-key", mode: int = 0o600) -> Path:
    path = home / ".config" / "sre-harness" / "nagiosgraph-key"
    path.write_text(text + "\n")
    path.chmod(mode)
    return path


def test_the_url_is_signed_over_host_service_period_timet_expires():
    url = chart.signed_url(PROXY, b"k", ("k8s03", "Load Average", "day", 1000), 2000)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    expires = 2000 + chart.GRAPH_TTL
    expected = hmac.new(b"k", f"k8s03|Load Average|day|1000|{expires}".encode(), hashlib.sha256).hexdigest()
    assert url.startswith("https://status.example.com/api/graph?")
    assert query["sig"] == [expected]
    assert query["service"] == ["Load Average"]
    assert "Load%20Average" in url


def test_a_key_file_others_can_read_is_refused(isolated: Path):
    write_key(isolated, mode=0o644)
    with pytest.raises(SiteError, match="owner only"):
        chart.signing_key("nagiosgraph-key")


def test_a_key_file_that_is_a_link_is_refused(isolated: Path, tmp_path: Path):
    real = tmp_path / "real-key"
    real.write_text("k\n")
    real.chmod(0o600)
    (isolated / ".config" / "sre-harness" / "nagiosgraph-key").symlink_to(real)
    with pytest.raises(SiteError, match="regular file"):
        chart.signing_key("nagiosgraph-key")


def nagios_args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    values = {"host": "k8s03", "service": "Load Average", "period": "day", "at": "now"}
    values.update(overrides)
    return argparse.Namespace(output=str(tmp_path / "tmp" / "g.png"), **values)


def test_nagios_writes_a_real_graph(isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_key(isolated)
    fetched: list[str] = []
    monkeypatch.setattr(chart, "fetch_graph", lambda url: fetched.append(url) or png(600, 225))
    result = chart.cmd_nagios(nagios_args(tmp_path), nagios_doc(tmp_path))
    assert Path(result["file"]).read_bytes() == png(600, 225)
    assert "s3cret" not in fetched[0]


def test_nagios_no_data_placeholder_is_no_data(isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_key(isolated)
    monkeypatch.setattr(chart, "fetch_graph", lambda url: png(608, 23))
    with pytest.raises(chart.NoData):
        chart.cmd_nagios(nagios_args(tmp_path), nagios_doc(tmp_path))
    assert not (tmp_path / "tmp" / "g.png").exists()


def test_nagios_refuses_a_response_that_is_not_a_png(isolated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_key(isolated)
    monkeypatch.setattr(chart, "fetch_graph", lambda url: b"<html>login</html>")
    with pytest.raises(SiteError, match="did not send a PNG"):
        chart.cmd_nagios(nagios_args(tmp_path), nagios_doc(tmp_path))


@pytest.mark.parametrize("host", ["k8s03;id", "-o", "a b", ""])
def test_nagios_refuses_a_host_that_is_not_a_name(tmp_path: Path, host: str):
    with pytest.raises(SiteError, match="not a host name"):
        chart.cmd_nagios(nagios_args(tmp_path, host=host), nagios_doc(tmp_path))


def test_nagios_needs_a_graph_proxy(tmp_path: Path):
    with pytest.raises(SiteError, match="graph_proxy"):
        chart.cmd_nagios(nagios_args(tmp_path), doc_with(tmp_path))


def test_main_exits_3_for_no_data(site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chart, "cmd_prom", lambda args, doc: (_ for _ in ()).throw(chart.NoData("empty")))
    out = str(tmp_path / "tmp" / "x.png")
    assert chart.main(["--site", str(site), "prom", "up", "-o", out]) == chart.NO_DATA
