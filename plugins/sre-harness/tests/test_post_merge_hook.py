"""The post-merge hook runs under a real 'git pull' and can never make one fail."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "templates" / "site-repo" / ".githooks" / "post-merge"


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def clone(tmp_path):
    """A clone with the hook installed the recommended way (copied into .git/hooks), and a way to push."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "T")
    (seed / ".githooks").mkdir()
    (seed / ".githooks" / "post-merge").write_bytes(HOOK.read_bytes())
    (seed / "a.txt").write_text("1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "init")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    installed = work / ".git" / "hooks" / "post-merge"
    installed.write_bytes(HOOK.read_bytes())
    installed.chmod(installed.stat().st_mode | stat.S_IXUSR)

    def advance(path: str = "a.txt", content: str = "x\n") -> None:
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((target.read_text() if target.exists() else "") + content)
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "more")
        git(seed, "push", "origin", "main")

    return work, advance


def stub_uv(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    """A 'uv' that records how it was called, then exits with a chosen code."""
    bin_dir, log = tmp_path / "bin", tmp_path / "uv.log"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\necho "SITE=$SRE_HARNESS_SITE CWD=$(pwd) ARGS=$*" >> "{log}"\nexit {exit_code}\n')
    uv.chmod(0o755)
    return bin_dir, log


def plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "incident.py").write_text("# stand-in\n")
    return root


def pull(work: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {"PATH": f"{env_extra.pop('BIN', '')}:/usr/bin:/bin", "HOME": str(work.parent), **env_extra}
    return subprocess.run(
        ["git", "-C", str(work), "pull", "--ff-only"], capture_output=True, text=True, env=env, check=False
    )


def test_a_pull_runs_gc_from_the_plugin_directory_ignoring_uv_config(clone, tmp_path):
    work, advance = clone
    advance()
    bin_dir, log = stub_uv(tmp_path)
    plugin_dir = plugin(tmp_path)
    done = pull(work, {"BIN": str(bin_dir), "SRE_HARNESS_PLUGIN": str(plugin_dir)})
    assert done.returncode == 0
    call = log.read_text()
    assert f"SITE={work.resolve()}" in call
    assert f"CWD={plugin_dir.resolve()}" in call, "not run from the repository"
    assert f"ARGS=run --no-config --no-project {plugin_dir}/scripts/incident.py gc --apply --no-fetch --quiet" in call


def test_the_plugin_can_come_from_per_clone_git_config(clone, tmp_path):
    work, advance = clone
    advance()
    bin_dir, log = stub_uv(tmp_path)
    git(work, "config", "sre-harness.plugin", str(plugin(tmp_path)))
    assert pull(work, {"BIN": str(bin_dir)}).returncode == 0
    assert "gc --apply" in log.read_text()


def test_the_plugin_is_never_guessed_from_the_plugin_cache(clone, tmp_path):
    work, advance = clone
    advance()
    bin_dir, log = stub_uv(tmp_path)
    cache = tmp_path / ".claude" / "plugins" / "cache" / "some-marketplace" / "sre-harness" / "9.9.9"
    (cache / "scripts").mkdir(parents=True)
    (cache / "scripts" / "incident.py").write_text("# someone else's copy\n")
    done = pull(work, {"BIN": str(bin_dir)})
    assert done.returncode == 0 and not log.exists(), "with nothing configured the hook does nothing"


def test_a_pull_cannot_change_the_installed_hook(clone, tmp_path):
    work, advance = clone
    installed = (work / ".git" / "hooks" / "post-merge").read_bytes()
    advance(".githooks/post-merge", "touch /tmp/pwned-by-a-pull\n")
    bin_dir, _log = stub_uv(tmp_path)
    assert pull(work, {"BIN": str(bin_dir), "SRE_HARNESS_PLUGIN": str(plugin(tmp_path))}).returncode == 0
    assert (work / ".git" / "hooks" / "post-merge").read_bytes() == installed
    assert "pwned" in (work / ".githooks" / "post-merge").read_text(), "the tracked copy did change"
    assert not Path("/tmp/pwned-by-a-pull").exists()


def test_a_failing_gc_never_fails_the_pull(clone, tmp_path):
    work, advance = clone
    advance()
    bin_dir, log = stub_uv(tmp_path, exit_code=3)
    done = pull(work, {"BIN": str(bin_dir), "SRE_HARNESS_PLUGIN": str(plugin(tmp_path))})
    assert done.returncode == 0 and log.exists()
    assert (work / "a.txt").read_text().endswith("x\n"), "the pull itself still happened"


@pytest.mark.parametrize("missing", ["plugin", "uv"])
def test_a_missing_plugin_or_uv_is_silently_skipped(clone, tmp_path, missing):
    work, advance = clone
    advance()
    bin_dir, log = stub_uv(tmp_path)
    extra = {"BIN": str(bin_dir) if missing == "plugin" else "", "SRE_HARNESS_PLUGIN": str(plugin(tmp_path))}
    if missing == "plugin":
        extra["SRE_HARNESS_PLUGIN"] = str(tmp_path / "nowhere")
    done = pull(work, extra)
    assert done.returncode == 0 and not log.exists()


def test_the_hook_is_executable_and_posix_sh():
    assert HOOK.stat().st_mode & stat.S_IXUSR
    assert HOOK.read_text().startswith("#!/bin/sh\n")
    assert os.access(HOOK, os.X_OK)
