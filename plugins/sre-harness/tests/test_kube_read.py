"""Tests for kube_read.py: only read verbs and listed options, in plain form."""

from __future__ import annotations

import re
from types import SimpleNamespace

import kube_read
import pytest


@pytest.mark.parametrize(
    "argv",
    [
        ["get", "pods", "-n", "web", "-o", "wide"],
        ["get", "pods", "--namespace=web", "--output=yaml"],
        ["get", "pods", "-A", "-l", "app=web", "--field-selector=status.phase=Running"],
        ["get", "deploy/web", "-o", "jsonpath={.spec.replicas}"],
        ["get", "pods", "-o", "custom-columns=NAME:.metadata.name"],
        ["get", "pods", "-o", "name"],
        ["describe", "pod", "web-1", "-n", "web"],
        ["logs", "web-1", "-c", "app", "--since", "10m", "--tail", "200", "-p"],
        ["top", "pods", "-n", "web", "--containers"],
        ["events", "-n", "web", "--types", "Warning"],
        ["get", "pods", "--context", "lab"],
        ["config", "current-context"],
        ["config", "get-contexts"],
        ["version", "--client"],
    ],
)
def test_reads_are_allowed(argv):
    assert kube_read.check(argv) == argv


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (["delete", "pod", "web-1"], "verb"),
        (["exec", "web-1", "--", "sh"], "verb"),
        (["config", "view", "--raw"], "verb"),
        (["get", "pods", "--server", "https://elsewhere"], "--server"),
        (["get", "pods", "-s", "https://elsewhere"], "-s"),
        (["get", "pods", "-As", "https://elsewhere"], "bundled"),
        (["get", "pods", "-nweb"], "bundled"),
        (["get", "pods", "--token=abc"], "--token"),
        (["get", "pods", "--kubeconfig", "/tmp/k"], "--kubeconfig"),
        (["get", "pods", "--as", "system:admin"], "--as"),
        (["get", "pods", "--as-group=system:masters"], "--as-group"),
        (["get", "pods", "--user", "admin"], "--user"),
        (["get", "pods", "--cluster", "other"], "--cluster"),
        (["get", "--raw", "/api/v1/namespaces/web/secrets"], "--raw"),
        (["get", "secrets", "-n", "web"], "secrets"),
        (["get", "pods,Secrets"], "secrets"),
        (["describe", "secret/db"], "secrets"),
        (["get", "pods", "-w"], "-w"),
        (["logs", "web-1", "-f"], "-f"),
        (["logs", "web-1", "--follow"], "--follow"),
        (["get", "pods", "-o", "go-template-file=/etc/passwd"], "output format"),
        (["get", "pods", "-o", "templatefile=/etc/passwd"], "output format"),
        (["get", "pods", "-o", "template=/x"], "output format"),
        (["get", "pods", "-o", "jsonpath-file=/x"], "output format"),
        (["get", "pods", "--output=custom-columns-file=/x"], "output format"),
        (["get", "pods", "--all-namespaces=false"], "takes no value"),
        (["get", "pods", "-n"], "needs a value"),
        (["get", "pods", "--", "x"], "'--'"),
        (["get", "pods; rm -rf /"], "characters"),
        (["get", "pods", "-l", "app=$(id)"], "characters"),
        ([], "verb"),
    ],
)
def test_everything_else_is_refused(argv, why):
    with pytest.raises(kube_read.Refused, match=re.escape(why)):
        kube_read.check(argv)


def test_refusal_exits_2_and_runs_nothing(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(kube_read.subprocess, "run", lambda *a, **k: ran.append(a))
    assert kube_read.main(["get", "pods", "--server", "https://elsewhere"]) == 2
    assert ran == []
    assert "REFUSED" in capsys.readouterr().err


def test_allowed_command_runs_as_an_argument_list(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(kube_read.shutil, "which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.setattr(kube_read.subprocess, "run", fake_run)
    assert kube_read.main(["get", "pods", "-n", "web"]) == 0
    assert seen["args"] == ["/usr/bin/kubectl", "get", "pods", "-n", "web"]
    assert "shell" not in seen["kwargs"]
