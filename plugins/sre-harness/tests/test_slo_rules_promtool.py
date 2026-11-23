"""Evaluate the generated probe-SLO recording rules with `promtool test rules`.

The string tests in test_slo.py pin what slo.py generates. These tests pin what the rules
do when Prometheus evaluates them against probe samples (claude-plugins#67): each sample
counts once in a window, however early in the SLO's life it is.

Needs promtool. It is found on PATH or through SRE_PROMTOOL, and the tests are skipped when
it is missing; CI installs a pinned, checksum-verified copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import slo
import yaml

PROMTOOL = os.environ.get("SRE_PROMTOOL") or shutil.which("promtool")

if PROMTOOL is None and os.environ.get("CI"):
    # A silent skip in CI would leave the rules unchecked while the build stays green.
    raise RuntimeError("promtool is required in CI: the install step failed or SRE_PROMTOOL is unset")

pytestmark = pytest.mark.skipif(PROMTOOL is None, reason="promtool is not installed (set SRE_PROMTOOL)")

PROBE = 'probe_success{job="canary"}'
LABELS = {"slo": "ex", "service": "svc", "journey": "j", "budget": "true"}


def sample(name: str) -> str:
    inner = ",".join(f'{key}="{value}"' for key, value in LABELS.items())
    return f"{name}{{{inner}}}"


def expectation(name: str, eval_time: str, value: float | None) -> dict:
    """One promql_expr_test: a value, or None for 'nothing recorded'."""
    case: dict = {"expr": sample(name), "eval_time": eval_time}
    if value is not None:
        case["exp_samples"] = [{"labels": sample(name), "value": value}]
    return case


def run_promtool(tmp_path: Path, series: str, expects: list[dict], window: str = "1d") -> None:
    """Write the rules for one probe SLO and a test of them, and run promtool."""
    probe = slo.Slo("ex", "svc", "j", 0.99, window, False, PROBE, PROBE, True, None, "p")
    (tmp_path / "rules.yml").write_text(
        yaml.safe_dump({"groups": [slo.rule_group(probe)]}, sort_keys=False, width=400), encoding="utf-8"
    )
    test = {
        "rule_files": ["rules.yml"],
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": [{"series": PROBE, "values": series}, {"series": 'other{job="x"}', "values": "1x400"}],
                "promql_expr_test": expects,
            }
        ],
    }
    (tmp_path / "test.yml").write_text(yaml.safe_dump(test, sort_keys=False), encoding="utf-8")
    done = subprocess.run(
        [PROMTOOL, "test", "rules", "test.yml"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_one_failure_in_a_new_slo_counts_once(tmp_path):
    # The nfs-storage case: the first probe fails, 114 pass. The failure is 1 of 115 samples
    # (0.87%), not the 2.3 samples' worth the averaged five-minute ratios gave it.
    run_promtool(
        tmp_path,
        "0 1x114",
        [
            expectation("slo:sli_error:ratio_rate1d", "114m", 1 / 115),
            expectation("slo:sli_error:ratio_rate1h", "114m", 0.0),  # the failure left the 1h window
            expectation("slo:sli_error:ratio_rate1h", "30m", 1 / 31),  # while it was still inside it
        ],
    )


def test_a_failure_in_the_first_minutes_is_not_amplified(tmp_path):
    # Ten minutes in, one failure among 11 samples is 1/11, never the 100% the first, nearly
    # empty five-minute windows used to hand it.
    run_promtool(tmp_path, "0 1x20", [expectation("slo:sli_error:ratio_rate1d", "10m", 1 / 11)])


def test_each_sample_is_counted_exactly_once(tmp_path):
    run_promtool(
        tmp_path,
        "1x59 0 1x59",
        [
            expectation("slo:sli_total:count1m", "30m", 1.0),
            expectation("slo:sli_bad:count1m", "30m", 0.0),
            expectation("slo:sli_bad:count1m", "59m", 0.0),
            expectation("slo:sli_bad:count1m", "60m", 1.0),
            expectation("slo:sli_bad:count1m", "61m", 0.0),
            expectation("slo:sli_error:ratio_rate1d", "119m", 1 / 120),
        ],
    )


def test_a_healthy_probe_has_no_error(tmp_path):
    run_promtool(
        tmp_path,
        "1x120",
        [expectation("slo:sli_error:ratio_rate1d", "90m", 0.0), expectation("slo:sli_error:ratio_rate1h", "90m", 0.0)],
    )


def test_a_missed_scrape_is_not_a_failure(tmp_path):
    # Every third sample is missing. Nothing is recorded for the empty minutes, and the window
    # counts only the probes that happened.
    series = " ".join(["1 1 _"] * 40)
    run_promtool(
        tmp_path,
        series,
        [
            expectation("slo:sli_total:count1m", "5m", None),  # minute 5 is a gap
            expectation("slo:sli_total:count1m", "6m", 1.0),
            expectation("slo:sli_error:ratio_rate1d", "100m", 0.0),
        ],
    )


def test_silence_after_reporting_counts_as_failure_once_five_minutes_pass(tmp_path):
    # Samples 0-9, then silence. A minute is a failed probe once no sample is left in the last
    # five minutes (from minute 14), and the first minutes of silence are not failures yet.
    run_promtool(
        tmp_path,
        "1x9 _x39",
        [
            expectation("slo:sli_bad:count1m", "9m", 0.0),
            expectation("slo:sli_bad:count1m", "10m", None),
            expectation("slo:sli_bad:count1m", "13m", None),
            expectation("slo:sli_bad:count1m", "14m", 1.0),
            expectation("slo:sli_total:count1m", "14m", 1.0),
            expectation("slo:sli_bad:count1m", "40m", 1.0),
            # 10 healthy samples, then failed minutes 14..40 (27 of them)
            expectation("slo:sli_error:ratio_rate1d", "40m", 27 / 37),
        ],
    )


def test_a_probe_that_never_reported_records_nothing(tmp_path):
    # The probe series does not exist (the other series stands in for a working backend), so
    # the state stays unknown instead of reading exhausted (claude-plugins#61).
    probe = slo.Slo(
        "ex", "svc", "j", 0.99, "1d", False, 'absent_probe{job="none"}', 'absent_probe{job="none"}', True, None, "p"
    )
    (tmp_path / "rules.yml").write_text(
        yaml.safe_dump({"groups": [slo.rule_group(probe)]}, sort_keys=False, width=400), encoding="utf-8"
    )
    never = [
        {"expr": sample(name), "eval_time": "60m"}
        for name in (
            "slo:sli_total:count1m",
            "slo:sli_bad:count1m",
            "slo:sli_error:ratio_rate5m",
            "slo:sli_error:ratio_rate1h",
            "slo:sli_error:ratio_rate1d",
        )
    ]
    test = {
        "rule_files": ["rules.yml"],
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": [{"series": 'other{job="x"}', "values": "1x400"}],
                "promql_expr_test": never,
            }
        ],
    }
    (tmp_path / "test.yml").write_text(yaml.safe_dump(test, sort_keys=False), encoding="utf-8")
    done = subprocess.run(
        [PROMTOOL, "test", "rules", "test.yml"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
