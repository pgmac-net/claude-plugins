"""Image primitives shared by share.py, chart.py, audit.py and state_gc.py (ADR 0027).

Kept apart from share.py so the audit log's commit can take an incident's images
without importing the chat policy (share -> chat_policy -> audit would be a cycle).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import sre_common

TYPES = ("png", "jpeg", "gif", "webp")


def sniff(head: bytes) -> str | None:
    """The image type its first bytes declare, or None. The name is never consulted."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def incident_images_dir(incident_id: str) -> Path:
    """Where the gateway keeps the images it posted for an incident, until audit.py commit takes them."""
    return sre_common.state_dir() / "images" / incident_id


def named_by_content(path: Path) -> bytes | None:
    """The bytes of a regular image file named '<sha256>.<type>' that matches its name; None otherwise."""
    digest, _, kind = path.name.partition(".")
    if path.is_symlink() or not path.is_file() or kind not in TYPES:
        return None
    data = path.read_bytes()
    if sniff(data[:16]) != kind or hashlib.sha256(data).hexdigest() != digest:
        return None
    return data
