"""Tests for shared helpers in sre_common.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import sre_common


def failing_gh(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(sre_common.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        sre_common.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    )


def test_gh_error_names_the_cause_not_the_end_of_a_usage_listing(monkeypatch):
    """An old gh answers 'gh label create' with a usage listing that ends in 'workflow'."""
    failing_gh(monkeypatch, 'unknown command "label" for "gh"\n\nUsage:  gh <command>\n  ...\n  workflow\n')
    with pytest.raises(sre_common.SiteError) as error:
        sre_common.run_gh(["label", "create", "drill"])
    assert 'unknown command "label"' in str(error.value)


def test_gh_error_keeps_a_single_line(monkeypatch):
    failing_gh(monkeypatch, "HTTP 404: Not Found (https://api.github.com/repos/x/y/labels)\n")
    with pytest.raises(sre_common.SiteError, match=r"failed: HTTP 404: Not Found \(https://api"):
        sre_common.run_gh(["label", "create", "drill"])


def test_gh_error_with_no_output(monkeypatch):
    failing_gh(monkeypatch, "")
    with pytest.raises(sre_common.SiteError, match="no output"):
        sre_common.run_gh(["issue", "view", "1"])


def _site_dir(path):
    path.mkdir(parents=True)
    (path / "site.yaml").write_text("schema_version: 7\n", encoding="utf-8")
    return path


def test_from_chat_site_may_name_only_the_gateways_own_site(tmp_path):
    own, other = _site_dir(tmp_path / "own"), _site_dir(tmp_path / "other")
    env = {sre_common.SITE_ENV: str(own), sre_common.NO_CURRENT_ENV: "1"}
    assert sre_common.resolve_site(str(own), env, tmp_path) == own.resolve()
    with pytest.raises(sre_common.SiteError, match="gateway's own site"):
        sre_common.resolve_site(str(other), env, tmp_path)


def test_outside_chat_site_may_name_any_site(tmp_path):
    own, other = _site_dir(tmp_path / "own"), _site_dir(tmp_path / "other")
    assert sre_common.resolve_site(str(other), {sre_common.SITE_ENV: str(own)}, tmp_path) == other.resolve()
