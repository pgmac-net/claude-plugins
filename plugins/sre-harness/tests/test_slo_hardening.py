"""slo.py must not send any file but a token to the backend, nor the token anywhere but the backend."""

from __future__ import annotations

import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
import slo
import validate_site as vs
from sre_common import SiteError
from test_slo import JOURNEY, REFS, SLO_SETTINGS, site_doc, slo_site  # noqa: F401  pylint: disable=unused-import


def private_token(tmp_path, content="glc_abcdefghijklmnop\n", mode=0o600):
    folder = tmp_path / "sre-harness"
    folder.mkdir(exist_ok=True)
    token = folder / "metrics-token"
    token.write_text(content, encoding="utf-8")
    token.chmod(mode)
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def test_token_file_must_be_private(tmp_path):
    env = private_token(tmp_path, mode=0o644)
    with pytest.raises(SiteError, match="owner only"):
        slo.read_token({"token_file": "metrics-token"}, env)
    (tmp_path / "sre-harness" / "metrics-token").chmod(0o600)
    assert slo.read_token({"token_file": "metrics-token"}, env) == "glc_abcdefghijklmnop"


def test_token_file_is_read_only_from_the_config_dir(tmp_path):
    env = private_token(tmp_path)
    assert slo.token_dir(env) == tmp_path / "sre-harness"


def test_token_file_may_not_be_a_link(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    (tmp_path / "sre-harness").mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATEKEY", encoding="utf-8")
    secret.chmod(0o600)
    (tmp_path / "sre-harness" / "metrics-token").symlink_to(secret)
    with pytest.raises(SiteError, match="regular file"):
        slo.read_token({"token_file": "metrics-token"}, env)


def test_token_must_be_one_token_shaped_line(tmp_path):
    env = private_token(tmp_path, content="line one\nline two\n")
    with pytest.raises(SiteError, match="token-shaped"):
        slo.read_token({"token_file": "metrics-token"}, env)


@pytest.mark.parametrize("value", ["/etc/passwd", "../outside", "~/.ssh/key", "a/b"])
def test_token_file_setting_is_a_bare_name(edit_site, value):
    def mutate(doc):
        doc["slo"] = {**SLO_SETTINGS, "token_file": value}

    assert any("token_file" in err for err in vs.validate(edit_site(mutate)).errors)


@pytest.mark.parametrize("field", ["rules_file", "journeys_path"])
@pytest.mark.parametrize("value", ["/etc/cron.d/x.yaml", "../escape.yaml", "slo/../../x.yaml", "~/x.yaml"])
def test_site_paths_stay_inside_the_repository(edit_site, field, value):
    def mutate(doc):
        doc["slo"] = {**SLO_SETTINGS, field: value}

    assert any(field in err for err in vs.validate(edit_site(mutate)).errors)


@pytest.mark.parametrize("url", ["http://metrics.example.com/api", "ftp://x", "https://user" + ":pw@host/api"])
def test_backend_url_must_be_https(edit_site, url):
    def mutate(doc):
        doc["slo"] = {**SLO_SETTINGS, "url": url}

    assert any("slo/url" in err for err in vs.validate(edit_site(mutate)).errors)


def test_plain_http_is_allowed_to_localhost(edit_site):
    def mutate(doc):
        doc["slo"] = {**SLO_SETTINGS, "url": "http://localhost:9090"}

    root = edit_site(mutate)
    (root / "catalog" / "journeys").mkdir()
    assert not any("slo/url" in err for err in vs.validate(root).errors)


def test_rules_file_link_is_refused(slo_site, tmp_path):  # noqa: F811
    target = tmp_path / "elsewhere.yaml"
    target.write_text("", encoding="utf-8")
    (slo_site / "slo").mkdir()
    (slo_site / "slo" / "rules.yaml").symlink_to(target)
    assert slo.main(["--site", str(slo_site), "generate"]) == 2
    assert target.read_text(encoding="utf-8") == ""


def test_linked_journey_files_are_errors(slo_site, tmp_path):  # noqa: F811
    outside = tmp_path / "outside.md"
    outside.write_text(JOURNEY, encoding="utf-8")
    (slo_site / "catalog" / "journeys" / "example-service" / "linked.md").symlink_to(outside)
    errors = slo.load(slo_site, site_doc(slo_site), REFS).errors
    assert any("links are not allowed" in err for err in errors)


def test_redirects_are_refused():
    request = urllib.request.Request("https://metrics.example.com/api/v1/query")
    with pytest.raises(urllib.error.HTTPError, match="redirect refused"):
        slo._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://elsewhere/")  # pylint: disable=protected-access


def test_token_header_is_never_forwarded_on_a_redirect(monkeypatch):
    seen = {}

    def capture(request, **_kwargs):
        seen["redirectable"] = dict(request.headers)
        seen["unredirected"] = dict(request.unredirected_hdrs)
        raise urllib.error.HTTPError(request.full_url, 302, "redirect refused", {}, None)

    monkeypatch.setattr(slo, "_OPENER", SimpleNamespace(open=capture))
    with pytest.raises(SiteError, match="HTTP 302"):
        slo.query(SLO_SETTINGS, "up", "very-secret-token")
    assert "Authorization" not in seen["redirectable"]
    assert "Authorization" in seen["unredirected"]
