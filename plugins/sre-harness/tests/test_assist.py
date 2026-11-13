"""Tests for assist.py. The service call is replaced; nothing leaves the machine."""

from __future__ import annotations

import json

import assist
import audit_hook
import pytest
import sre_common
import yaml

KEY_ENV = "HELPER_KEY"
PRIVATE_IP = ".".join(["192", "168", "7", "20"])


@pytest.fixture
def enabled(site, monkeypatch, tmp_path):
    """A site with the helper on, a key in the environment, a mask file and two runbooks."""
    monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv(KEY_ENV, "k" * 40)
    runbooks = tmp_path / "incidents" / "docs" / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "disk-full.md").write_text("---\ntags: [x]\n---\n# Disk full\n\nThe volume fills and writes fail.\n")
    (runbooks / "dns-down.md").write_text("# DNS down\n\nNames stop resolving on buildbox01.\n")
    (runbooks / "index.md").write_text("# Index\n")
    (site / "site-identifiers.txt").write_text("# real names\nbuildbox01\ncorp.example.org\n", encoding="utf-8")
    doc = yaml.safe_load((site / "site.yaml").read_text())
    doc["repos"]["incidents"]["path"] = str(tmp_path / "incidents")
    doc["incident"]["assist"] = {
        "provider": "jev",
        "enabled": True,
        "api_key_env": KEY_ENV,
        "mask_file": "site-identifiers.txt",
        "model": "jev-test",
    }
    (site / "site.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return site


@pytest.fixture
def service(monkeypatch):
    """Record what would be sent, and answer with the first option."""
    sent: list[dict] = []

    def fake(assist_settings, state, instructions, options):
        sent.append(
            {"state": state, "instructions": instructions, "options": options, "model": assist_settings.get("model")}
        )
        labels = list(options)
        probabilities = {label: 0.0 for label in labels}
        probabilities[labels[0]] = 0.7
        if len(labels) > 1:
            probabilities[labels[1]] = 0.3
        return assist.checked(
            {"choice": labels[0], "confidence": 0.61, "probabilities": probabilities, "model": "jev-test"}, options
        )

    monkeypatch.setattr(assist, "ask", fake)
    return sent


def run(capsys, *argv: str) -> dict:
    assert assist.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


class TestUnavailable:
    def test_off_by_default_in_the_template(self, site, monkeypatch, capsys):
        monkeypatch.setenv("SRE_HARNESS_SITE", str(site))
        result = run(capsys, "severity", "--impact", "Shop is down")
        assert result == {
            "available": False,
            "advisory": True,
            "kind": "severity",
            "reason": "the advisory helper is not enabled for this site",
        }

    @pytest.mark.usefixtures("enabled", "service")
    def test_no_key_means_no_call(self, monkeypatch, capsys, service):
        monkeypatch.delenv(KEY_ENV)
        result = run(capsys, "severity", "--impact", "Shop is down")
        assert result["available"] is False and KEY_ENV in result["reason"]
        assert service == []

    @pytest.mark.usefixtures("service")
    def test_missing_mask_file_refuses_to_send(self, enabled, capsys, service):
        (enabled / "site-identifiers.txt").unlink()
        result = run(capsys, "severity", "--impact", "Shop is down")
        assert result["available"] is False and "not sending unmasked text" in result["reason"]
        assert service == []

    @pytest.mark.usefixtures("enabled")
    def test_service_failure_is_reported_not_raised(self, monkeypatch, capsys):
        def broken(*_args):
            raise assist.Unavailable("the helper's service did not answer: Timeout")

        monkeypatch.setattr(assist, "ask", broken)
        result = run(capsys, "entry-type", "--text", "Deleted the pod")
        assert result["available"] is False and "did not answer" in result["reason"]

    def test_no_site_is_reported_not_raised(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("SRE_HARNESS_SITE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
        assert run(capsys, "entry-type", "--text", "x")["available"] is False


@pytest.mark.usefixtures("enabled")
class TestMasking:
    def test_identifiers_addresses_and_secrets_never_leave(self, capsys, service):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        impact = f"BuildBox01 at {PRIVATE_IP} cannot reach db.corp.example.org; token {token}; buildbox01 again"
        run(capsys, "severity", "--impact", impact, "--affected", "buildbox01")
        wire = json.dumps(service[0])
        for secret in ("buildbox01", "BuildBox01", "corp.example.org", PRIVATE_IP, token):
            assert secret not in wire
        sent_impact = service[0]["state"]["impact"]
        assert sent_impact == "site-2 at address-1 cannot reach db.site-1; token [REDACTED:GitHub token]; site-2 again"
        assert service[0]["state"]["affected"] == "site-2", "the same identifier keeps one placeholder"

    def test_runbook_text_is_masked_and_file_names_are_not_sent(self, capsys, service):
        run(capsys, "runbooks", "--symptoms", "names do not resolve")
        wire = json.dumps(service[0])
        assert "buildbox01" not in wire
        assert "dns-down.md" not in wire and "disk-full.md" not in wire
        assert set(service[0]["options"]) == {"r1", "r2", "none"}

    def test_the_key_itself_is_never_sent(self, capsys, service):
        run(capsys, "entry-type", "--text", "the key is " + "k" * 40)
        assert "k" * 40 not in json.dumps(service[0])

    def test_long_input_is_cut_and_nothing_derived_from_the_original_is_sent(self, capsys, service):
        run(capsys, "entry-type", "--text", "word " * 5000)
        sent = service[0]["state"]["entry"]
        assert len(sent) <= assist.MAX_FIELD
        assert "sha256" not in sent and "truncated" not in sent

    @pytest.mark.parametrize(
        "secret_text",
        [
            "db --password x-buildbox01-y9Zq end",
            "token=abc.buildbox01.def.ghi end",
            "curl -u admin:buildbox01-s3cret https://example.com end",
            "Authorization: Bearer abcd1234.buildbox01.wxyz5678 end",
        ],
    )
    def test_a_secret_that_contains_an_identifier_is_still_fully_redacted(self, capsys, service, secret_text):
        """Masking must not run first: it would change part of the secret and stop its pattern matching."""
        run(capsys, "entry-type", "--text", secret_text)
        sent = service[0]["state"]["entry"]
        for fragment in ("x-", "-y9Zq", "abc.", ".def.ghi", "-s3cret", "abcd1234", "wxyz5678", "buildbox01"):
            assert fragment not in sent, sent
        assert "[REDACTED:" in sent

    def test_env_secret_containing_an_identifier_is_redacted_whole(self, monkeypatch, capsys, service):
        monkeypatch.setenv("SHOP_API_TOKEN", "zz-buildbox01-qq-123456")
        run(capsys, "entry-type", "--text", "used zz-buildbox01-qq-123456 to log in")
        assert service[0]["state"]["entry"] == "used [REDACTED:env SHOP_API_TOKEN] to log in"

    @pytest.mark.parametrize("offset", [5, 15, 25, 39])
    def test_a_secret_straddling_the_cut_is_dropped_not_halved(self, capsys, service, offset):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        start = assist.MAX_FIELD - offset
        text = ("w " * 4000)[: start - 1] + " " + token + " tail tail tail"
        run(capsys, "entry-type", "--text", text)
        sent = service[0]["state"]["entry"]
        assert "ghp_" not in sent and token[:10] not in sent
        assert len(sent) <= assist.MAX_FIELD

    def test_an_unbroken_run_longer_than_the_limit_is_not_sent_at_all(self, capsys, service):
        blob = "A" * 3000 + "buildbox01" + "B" * 4000
        run(capsys, "entry-type", "--text", blob)
        assert service[0]["state"]["entry"] == assist.OMITTED

    def test_a_long_run_at_the_end_is_dropped_whole_however_long_it_is(self, capsys, service):
        """No fallback hard cut: a 2,000-character run crossing the limit goes entirely."""
        run_start = assist.MAX_FIELD - 1500
        text = ("w " * 4000)[: run_start - 1] + " " + "secretbuildbox01" * 200
        run(capsys, "entry-type", "--text", text)
        sent = service[0]["state"]["entry"]
        assert "secret" not in sent and "buildbox01" not in sent
        assert len(sent) <= run_start

    @pytest.mark.parametrize("space", ["\r", "\f", "\v", "\u00a0", "\n", "\t"])
    def test_any_whitespace_is_a_place_to_cut(self, capsys, service, space):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        text = "x" * (assist.MAX_FIELD - 20) + space + token + space + "tail"
        run(capsys, "entry-type", "--text", text)
        assert service[0]["state"]["entry"] == "x" * (assist.MAX_FIELD - 20)

    def test_a_huge_secret_early_cannot_pull_unscanned_text_into_view(self, capsys, service):
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        key = "-----BEGIN " + "PRIVATE KEY-----\n" + "A" * 5000 + "\n-----END " + "PRIVATE KEY-----"
        text = key + " " + ("w " * 1500) + token + " tail"
        run(capsys, "entry-type", "--text", text)
        sent = service[0]["state"]["entry"]
        assert "ghp_" not in sent and "AAAA" not in sent

    @pytest.mark.parametrize("position", [100, 5990, 6500, 7990, 12000])
    def test_an_identifier_anywhere_in_long_text_is_never_sent(self, capsys, service, position):
        text = ("word " * 4000)[:position] + " buildbox01 " + "tail " * 100
        run(capsys, "entry-type", "--text", text)
        assert "buildbox01" not in json.dumps(service[0])

    @pytest.mark.parametrize(
        "text",
        [
            "volume buildbox01-data is full",
            "see my_buildbox01 for logs",
            "path /srv/buildbox01/x and host buildbox01.",
            "BUILDBOX01_backup failed",
            "fqdn db.corp.example.org:5432 refused",
            "url https://buildbox01:8443/x",
        ],
    )
    def test_identifiers_joined_by_punctuation_are_masked(self, capsys, service, text):
        run(capsys, "entry-type", "--text", text)
        wire = json.dumps(service[0]).lower()
        assert "buildbox01" not in wire and "corp.example.org" not in wire

    def test_an_identifier_inside_a_longer_word_is_left_alone(self, capsys, service):
        run(capsys, "entry-type", "--text", "the rebuildbox012 tool")
        assert "rebuildbox012" in service[0]["state"]["entry"]

    @pytest.mark.parametrize(
        "address",
        [
            ".".join(["10", "1", "2", "3"]),
            ".".join(["172", "20", "0", "9"]),
            ".".join(["100", "64", "1", "2"]),
            ".".join(["100", "127", "250", "1"]),
            ".".join(["169", "254", "10", "10"]),
            "fd" + "12:3456:789a::1",
            "fe" + "80::1ff:fe23:4567:890a",
            "FC" + "00::1",
        ],
    )
    def test_private_addresses_of_every_family_are_masked(self, capsys, service, address):
        run(capsys, "entry-type", "--text", f"cannot reach {address} from here")
        assert address not in json.dumps(service[0])
        assert "address-1" in service[0]["state"]["entry"]

    @pytest.mark.parametrize("address", ["8.8.8.8", "100.63.0.1", "100.128.0.1", "2001:db8::1", "version 10.3.3"])
    def test_public_addresses_and_version_numbers_are_left_alone(self, capsys, service, address):
        run(capsys, "entry-type", "--text", f"probe to {address} failed")
        assert address in service[0]["state"]["entry"]


@pytest.mark.usefixtures("enabled")
class TestAdvice:
    def test_severity_uses_generic_criteria_without_a_site_document(self, capsys, service):
        result = run(capsys, "severity", "--impact", "Shop is down for everyone")
        assert result["available"] is True and result["advisory"] is True
        assert result["suggestion"] == "P1"
        assert list(result["probabilities"]) == ["P1", "P2", "P3", "P4"]
        assert result["used_site_criteria"] is False
        assert "provisional" in result["note"]
        assert service[0]["model"] == "jev-test"

    def test_severity_uses_the_sites_criteria_when_present(self, tmp_path, capsys, service):
        template = tmp_path / "incidents" / "docs" / "templates" / "pir-template.md"
        template.parent.mkdir(parents=True)
        template.write_text("P1 means buildbox01 is down.\n", encoding="utf-8")
        result = run(capsys, "severity", "--impact", "x")
        assert result["used_site_criteria"] is True
        assert service[0]["state"]["site_criteria"] == "P1 means site-1 is down.\n"
        assert "buildbox01" not in json.dumps(service[0])
        assert service[0]["options"]["P2"] == "P2 as defined in `site_criteria`."

    @pytest.mark.usefixtures("service")
    def test_runbooks_are_ranked_by_file_name_with_a_none_option(self, capsys):
        result = run(capsys, "runbooks", "--symptoms", "disk is full", "--top", "1")
        assert result["ranked"] == [{"runbook": "disk-full.md", "probability": 0.7}]
        both = run(capsys, "runbooks", "--symptoms", "disk is full")
        assert [item["runbook"] for item in both["ranked"]] == ["disk-full.md", "dns-down.md"], (
            "zero-probability ones are left out"
        )
        assert result["candidates"] == 2
        assert result["none_probability"] == 0.0
        assert "Read the top runbooks" in result["note"]

    def test_entry_type_offers_the_seven_types(self, capsys, service):
        result = run(capsys, "entry-type", "--text", "Found the disk at 100%")
        assert result["suggestion"] == "discovery"
        assert set(service[0]["options"]) == set(assist.ENTRY_TYPES)

    def test_an_answer_outside_the_options_is_refused(self):
        with pytest.raises(assist.Unavailable, match="not one of the options"):
            assist.checked({"choice": "run rm -rf /", "probabilities": {}}, {"a": "x"})

    def test_unknown_labels_and_odd_values_from_the_service_are_dropped(self):
        answer = {
            "choice": "a",
            "confidence": "high",
            "probabilities": {"a": 0.9, "evil": 1, "b": "x"},
            "model": "m\nm",
        }
        assert assist.checked(answer, {"a": "x", "b": "y"}) == {
            "choice": "a",
            "confidence": None,
            "probabilities": {"a": 0.9, "b": 0.0},
            "model": "unknown",
        }


@pytest.mark.usefixtures("enabled", "service")
class TestAuditTrail:
    def test_advice_is_recorded_during_an_incident(self, capsys):
        sre_common.write_incident_state({"id": "catalog-5", "repo": "o/r", "number": 5, "url": "u", "title": "T"})
        audit_hook.append("catalog-5", {"v": 1, "ts": "2026-01-02T03:04:05Z", "event": "note"})
        run(capsys, "severity", "--impact", "Shop is down")
        record = json.loads(audit_hook.spool_path("catalog-5").read_text().splitlines()[-1])
        assert (record["event"], record["kind"], record["suggestion"], record["capture"]) == (
            "assist",
            "severity",
            "P1",
            "self-reported",
        )
        assert "probabilities" not in record

    def test_nothing_is_recorded_outside_an_incident(self, capsys):
        run(capsys, "severity", "--impact", "Shop is down")
        assert not (audit_hook.state_dir() / "audit").exists()
