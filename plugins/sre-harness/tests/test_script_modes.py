"""Scripts meant to be run directly are executable, in the working tree and in git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((PLUGIN / "scripts").glob("*.py"))


def has_shebang(path: Path) -> bool:
    return path.read_text(encoding="utf-8").startswith("#!")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_is_executable_exactly_when_it_has_a_shebang(script):
    assert os.access(script, os.X_OK) == has_shebang(script), (
        f"{script.name}: executable bit and shebang disagree. Scripts run directly need both; libraries need neither."
    )


def test_git_records_the_same_modes():
    out = subprocess.run(
        ["git", "-C", str(PLUGIN), "ls-files", "-s", "scripts"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("not inside a git checkout")
    modes = {Path(line.split("\t")[1]).name: line.split()[0] for line in out.stdout.splitlines()}
    for script in SCRIPTS:
        expected = "100755" if has_shebang(script) else "100644"
        assert modes.get(script.name) == expected, f"{script.name} is {modes.get(script.name)} in git, want {expected}"
