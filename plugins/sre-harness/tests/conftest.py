"""Shared fixtures: make the plugin scripts importable and copy the site template."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_ROOT / "templates" / "site-repo"

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A writable copy of the site-repo template."""
    root = tmp_path / "site"
    shutil.copytree(TEMPLATE, root)
    return root


@pytest.fixture
def edit_site(site: Path):
    """Return a function that rewrites site.yaml through a mutator."""

    def _edit(mutate) -> Path:
        path = site / "site.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return site

    return _edit
