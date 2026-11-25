"""Tests for share.py: what may be handed to the chat gateway, and how the gateway trusts the spool (ADR 0027)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # noqa: S404 - git init in a temp directory
import time
from pathlib import Path

import audit
import chat_policy
import pytest
import share
import state_gc
import validate_site
from sre_common import SiteError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
DOC = {"chat": {}}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home and state directory of the test's own."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return home


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(("data", "kind"), [(PNG, "png"), (JPEG, "jpeg"), (GIF, "gif"), (WEBP, "webp")])
def test_an_image_is_known_by_its_content(data: bytes, kind: str):
    assert share.sniff(data[:16]) == kind


@pytest.mark.parametrize(
    "data",
    [b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.7", b"ssh-ed25519 AAAA", b"", b"RIFF\x00\x00\x00\x00WAVE"],
)
def test_anything_else_is_not_an_image(data: bytes):
    assert share.sniff(data[:16]) is None


def test_a_text_file_named_png_is_refused(tmp_path: Path):
    path = write(tmp_path / "graph.png", b"GRAFANA_TOKEN=glsa_abc\n")
    with pytest.raises(SiteError, match="not a PNG"):
        share.share(path, "load", DOC)


def test_an_image_over_the_limit_is_refused(tmp_path: Path):
    path = write(tmp_path / "big.png", PNG + b"\x00" * (1024 * 1024))
    with pytest.raises(SiteError, match="not shared"):
        share.share(path, "load", {"chat": {"images": {"max_mb": 1}}})


@pytest.mark.parametrize("relative", [".ssh/id.png", ".config/sre-harness/key.png", ".kube/x.png", "w/.env"])
def test_a_denied_location_is_refused_even_for_a_real_image(isolated: Path, relative: str):
    path = write(isolated / relative, PNG)
    with pytest.raises(SiteError, match="denies"):
        share.share(path, "x", DOC)


def test_a_link_into_a_denied_location_is_refused(isolated: Path, tmp_path: Path):
    target = write(isolated / ".ssh" / "id.png", PNG)
    link = tmp_path / "innocent.png"
    link.symlink_to(target)
    with pytest.raises(SiteError, match="denies"):
        share.share(link, "x", DOC)


def test_the_harness_state_directory_is_refused(tmp_path: Path):
    path = write(tmp_path / "state" / "sre-harness" / "shared" / "x.png", PNG)
    with pytest.raises(SiteError, match="denies"):
        share.share(path, "x", DOC)


def test_the_policy_deny_list_is_what_is_checked(isolated: Path):
    """Every home-relative deny path refuses a file inside it, so the two lists cannot drift apart."""
    for pattern in chat_policy.DENY_PATHS:
        if pattern.startswith("~/") and pattern.endswith("/**"):
            assert share.denied(isolated / pattern[2:-3] / "a.png"), pattern


def test_a_title_is_one_printable_line():
    assert share.clean_title("  k8s03\nload\x1b[31m  day ") == "k8s03 load [31m day"
    assert len(share.clean_title("x" * 500)) == share.MAX_TITLE
    with pytest.raises(SiteError, match="empty"):
        share.clean_title(" \n\t ")


def test_sharing_spools_the_image_privately_and_the_gateway_can_load_it(tmp_path: Path):
    path = write(tmp_path / "load.png", PNG)
    result = share.share(path, "k8s03 load, day", DOC)
    digest = hashlib.sha256(PNG).hexdigest()
    assert result == {"shared": digest, "type": "png", "bytes": len(PNG), "title": "k8s03 load, day"}
    image, meta = share.spool_paths(digest, "png")
    assert image.read_bytes() == PNG
    assert oct(image.stat().st_mode & 0o777) == "0o600"
    assert oct(share.spool_dir().stat().st_mode & 0o777) == "0o700"
    loaded = share.load_spooled(digest)
    assert loaded is not None
    assert loaded["path"] == str(image)
    assert loaded["title"] == "k8s03 load, day"
    assert json.loads(meta.read_text())["name"] == "load.png"


def test_the_gateway_does_not_trust_a_spool_entry_that_changed(tmp_path: Path):
    digest = share.share(write(tmp_path / "a.png", PNG), "a", DOC)["shared"]
    image, _ = share.spool_paths(digest, "png")
    image.write_bytes(PNG + b"tampered")
    assert share.load_spooled(digest) is None


def test_the_gateway_does_not_follow_a_link_in_the_spool(tmp_path: Path):
    digest = share.share(write(tmp_path / "a.png", PNG), "a", DOC)["shared"]
    image, _ = share.spool_paths(digest, "png")
    image.unlink()
    image.symlink_to(tmp_path / "a.png")
    assert share.load_spooled(digest) is None


@pytest.mark.parametrize("digest", ["", "../../etc/passwd", "A" * 64, "0" * 63])
def test_a_digest_that_is_not_one_loads_nothing(digest: str):
    assert share.load_spooled(digest) is None


def test_discard_removes_the_image_and_its_description(tmp_path: Path):
    digest = share.share(write(tmp_path / "a.png", PNG), "a", DOC)["shared"]
    share.discard(digest)
    assert list(share.spool_dir().iterdir()) == []


def test_prune_removes_only_old_spool_files(tmp_path: Path):
    digest = share.share(write(tmp_path / "a.png", PNG), "a", DOC)["shared"]
    old = time.time() - 2 * share.DAY
    for path in share.spool_dir().iterdir():
        os.utime(path, (old, old))
    share.share(write(tmp_path / "b.png", GIF), "b", DOC)
    names = {path.name for path in share.spool_dir().iterdir()}
    assert not any(name.startswith(digest) for name in names)
    assert len(names) == 2


def test_main_prints_one_json_line(site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = write(tmp_path / "a.png", PNG)
    assert share.main([str(path), "--title", "load", "--site", str(site)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert json.loads(first)["shared"] == hashlib.sha256(PNG).hexdigest()


def test_main_refuses_with_exit_2(site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = write(tmp_path / "a.png", b"not an image")
    assert share.main([str(path), "--title", "x", "--site", str(site)]) == 2
    assert "ERROR" in capsys.readouterr().err


# --- the record: audit commit, gc, site validation ------------------------------------------


def keep(incident_id: str, data: bytes, kind: str = "png") -> Path:
    digest = hashlib.sha256(data).hexdigest()
    return write(share.incident_images_dir(incident_id) / f"{digest}.{kind}", data)


def test_audit_commit_takes_the_incidents_images_beside_the_log(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True)  # noqa: S607
    kept = keep("incidents-7", PNG)
    write(share.incident_images_dir("incidents-7") / "notes.txt", b"not an image")
    write(share.incident_images_dir("incidents-7") / ("0" * 64 + ".png"), PNG)  # name does not match content
    assert audit._add_images(tree, Path("audit/incidents-7.jsonl"), "incidents-7") == 1  # pylint: disable=protected-access
    assert (tree / "audit" / "incidents-7.images" / kept.name).read_bytes() == PNG


def test_audit_commit_with_no_images_adds_nothing(tmp_path: Path):
    assert audit._add_images(tmp_path, Path("audit/x-1.jsonl"), "x-1") == 0  # pylint: disable=protected-access


def test_gc_counts_an_incidents_images_as_its_files(tmp_path: Path):
    kept = keep("incidents-7", PNG)
    assert kept in state_gc.files_of(tmp_path / "state" / "sre-harness", "incidents-7")


def test_a_graph_proxy_belongs_only_to_a_nagios_source():
    doc = {
        "repos": {"catalog": {"repo": "o/r", "visibility": "private"}},
        "audit": {"repo": "catalog"},
        "monitoring": [{"name": "m", "type": "prometheus", "graph_proxy": {"url": "https://x", "key_file": "k"}}],
    }
    assert "monitoring/0/graph_proxy: only a nagios source has a graph proxy" in validate_site.check_references(doc)


@pytest.mark.parametrize("builder", [chat_policy.rules, chat_policy.triage_rules, chat_policy.question_rules])
def test_chart_and_share_run_without_approval_in_every_chat_policy(builder):
    allow = builder({}, Path("/p/scripts"))["allow"]
    assert "Bash(uv run /p/scripts/share.py *)" in allow
    assert "Bash(uv run /p/scripts/chart.py *)" in allow
