"""Tests for check_identifiers.py.

Sample identifiers are assembled at runtime so this file itself stays clean
under the scan it tests.
"""

from __future__ import annotations

from pathlib import Path

import check_identifiers as ci
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

SAMPLES = {
    "private IP address": ".".join(["192", "168", "1", "20"]),
    "home directory path": "/" + "home" + "/alice/projects",
    "internal hostname": "status" + ".int." + "example.net",
    "chat ID": "U" + "02ABCDEF1",
}


def write(root: Path, text: str) -> Path:
    path = root / "notes.md"
    path.write_text(text + "\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("label", sorted(SAMPLES))
def test_generic_patterns_are_found(tmp_path, label):
    findings = ci.scan(write(tmp_path, f"see {SAMPLES[label]} for detail"), list(ci.GENERIC_PATTERNS))
    assert len(findings) == 1
    assert label in findings[0]
    assert findings[0].startswith("notes.md:1:")


@pytest.mark.parametrize(
    "text",
    [
        "documentation range 192.0.2.10 and 198.51.100.7",
        "https://monitoring.example.com/ and example.grafana.net",
        "paths like ~/src/incidents or /path/to/this/checkout",
        "version 10.3.3 of the tool",
        "CONTRIBUTING and UNKNOWN are plain words",
    ],
)
def test_reserved_examples_are_clean(tmp_path, text):
    assert ci.scan(write(tmp_path, text), list(ci.GENERIC_PATTERNS)) == []


def test_allow_marker_skips_the_line(tmp_path):
    line = f"{SAMPLES['private IP address']}  # {ci.ALLOW_MARKER}"
    assert ci.scan(write(tmp_path, line), list(ci.GENERIC_PATTERNS)) == []


def test_denylist_matches_whole_identifiers_case_insensitively(tmp_path):
    denylist = tmp_path / "deny.txt"
    denylist.write_text("# comment\nbuildbox01\n\nexample.org  # trailing comment\n", encoding="utf-8")
    patterns = ci.load_denylist(denylist)
    assert len(patterns) == 2

    root = tmp_path / "scan"
    root.mkdir()
    write(root, "ssh BuildBox01 then visit www.example.org; buildbox012 is unrelated")
    findings = ci.scan(root, patterns)
    assert len(findings) == 2
    assert all("denylisted identifier" in finding for finding in findings)


def test_main_exit_codes(tmp_path, capsys):
    assert ci.main([str(write(tmp_path, "nothing to see"))]) == 0
    write(tmp_path, SAMPLES["private IP address"])
    assert ci.main([str(tmp_path)]) == 1
    assert "private IP address" in capsys.readouterr().out


def test_plugin_itself_is_clean():
    """The portable plugin must carry no site identifiers."""
    assert ci.scan(PLUGIN_ROOT, list(ci.GENERIC_PATTERNS)) == []
