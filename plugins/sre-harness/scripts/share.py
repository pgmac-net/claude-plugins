#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.21", "pyyaml>=6"]
# ///
"""Hand an image to the chat gateway, which posts it to the conversation it came from.

The agent can draw a graph (chart.py, sargraph, a nagiosgraph PNG) but has no
way to put it in front of people in chat. This script checks the file and
copies it to a spool in the harness's state directory; the gateway sees the
call, finds the copy there and uploads it (ADR 0027). Outside the gateway
nothing reads the spool, so the image is only checked and kept for a day.

What is checked, because the call runs without an approval request:
- the file is an image by its content (PNG, JPEG, GIF or WebP magic bytes),
  never by its name: an SVG, a PDF or a text file renamed .png is refused;
- it is no larger than chat.images.max_mb in site.yaml (default 5 MB);
- its path, after following links, is not one the chat policy denies
  (credentials, the harness's own state and configuration).

An image cannot be redacted the way text is. Share what you drew or fetched
for this conversation, never an image of a secret.

Usage: share.py <file> --title "<caption naming host, metric and window>"
Prints one JSON line: {"shared": <sha256>, "type": <png|jpeg|gif|webp>, "bytes": <n>, "title": <caption>}.
Exit codes: 0 shared, 2 refused.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import chat_policy
import sre_common
from image_files import TYPES, incident_images_dir, named_by_content, sniff
from sre_common import SiteError

__all__ = ["TYPES", "denied", "discard", "incident_images_dir", "load_spooled", "share", "sniff"]

DEFAULT_MAX_MB = 5
MAX_TITLE = 200
SPOOL_DAYS = 1
DAY = 86400


def max_bytes(doc: dict[str, Any]) -> int:
    """The largest image that may be shared (chat.images.max_mb)."""
    images = (doc.get("chat") or {}).get("images") or {}
    return int(images.get("max_mb", DEFAULT_MAX_MB)) * 1024 * 1024


def _patterns(home: Path) -> list[str]:
    """The chat policy's denied paths as absolute shell patterns ('*' matches across '/')."""
    found = []
    for path in chat_policy.DENY_PATHS:
        if path.startswith("//"):
            found.append(path[1:])
        elif path.startswith("~/"):
            found.append(str(home / path[2:]))
        elif path.startswith("**/"):
            found.append("*/" + path[3:])
        else:
            found.append(path)
    return [pattern.removesuffix("/**") + "/*" if pattern.endswith("/**") else pattern for pattern in found]


def denied(path: Path, home: Path | None = None) -> bool:
    """Whether the chat policy denies reading this path. Checked on the path as given and with links followed."""
    home = Path.home() if home is None else home
    patterns = _patterns(home)
    candidates = {str(path.expanduser().absolute()), str(path.expanduser().resolve())}
    state = sre_common.state_dir()
    for candidate in candidates:
        if Path(candidate) == state or state in Path(candidate).parents:
            return True
        if any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns):
            return True
    return False


def clean_title(text: str) -> str:
    """One line of printable text, at most MAX_TITLE characters. Refused when nothing is left."""
    title = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    if not title:
        raise SiteError("--title is empty: say what the image shows (host, metric, window)")
    return title[:MAX_TITLE]


def read_image(path: Path, limit: int) -> tuple[bytes, str]:
    """The file's bytes and image type. Raises SiteError for anything that is not a small regular image."""
    if denied(path):
        raise SiteError(f"{path} is in a location the chat policy denies; it cannot be shared")
    try:
        info = path.resolve().stat()
    except OSError as exc:
        raise SiteError(f"{path} cannot be read: {type(exc).__name__}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SiteError(f"{path} is not a regular file")
    if info.st_size > limit:
        raise SiteError(f"{path} is {info.st_size} bytes; images over {limit} bytes are not shared")
    with path.resolve().open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise SiteError(f"{path} grew past {limit} bytes while it was read")
    kind = sniff(data[:16])
    if kind is None:
        raise SiteError(f"{path} is not a PNG, JPEG, GIF or WebP image (checked by content, not name)")
    return data, kind


def spool_dir() -> Path:
    """Where shared images wait for the gateway."""
    return sre_common.state_dir() / "shared"


def spool_paths(digest: str, kind: str) -> tuple[Path, Path]:
    """(the image, its description) in the spool."""
    return spool_dir() / f"{digest}.{kind}", spool_dir() / f"{digest}.json"


def _write_private(path: Path, data: bytes) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def prune(now: float | None = None) -> int:
    """Remove spooled files older than SPOOL_DAYS: nothing reads them outside the gateway."""
    directory = spool_dir()
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - SPOOL_DAYS * DAY
    removed = 0
    for path in directory.iterdir():
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def share(path: Path, title: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Check one image and spool it for the gateway. Returns the result line's fields."""
    caption = clean_title(title)
    data, kind = read_image(path, max_bytes(doc))
    digest = hashlib.sha256(data).hexdigest()
    directory = spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    image, meta = spool_paths(digest, kind)
    _write_private(image, data)
    description = {"sha256": digest, "type": kind, "bytes": len(data), "title": caption, "name": path.name[:120]}
    _write_private(meta, (json.dumps(description, sort_keys=True) + "\n").encode())
    prune()
    return {"shared": digest, "type": kind, "bytes": len(data), "title": caption}


def load_spooled(digest: str) -> dict[str, Any] | None:
    """A spooled image as the gateway trusts it: its description, re-checked against the bytes on disk.

    The script's printed output is not trusted: the image must be in the spool, still be an image of the
    type recorded, and still hash to its name. Returns the description with 'path', or None.
    """
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    _, meta = spool_paths(digest, "png")
    try:
        description = json.loads(meta.read_text(encoding="utf-8"))
        kind = description.get("type") if isinstance(description, dict) else None
        image, _ = spool_paths(digest, str(kind))
        data = named_by_content(image) if kind in TYPES else None
    except (OSError, ValueError):
        data = None
    if data is None:
        return None
    return {**description, "path": str(image), "bytes": len(data)}


def discard(digest: str) -> None:
    """Remove one image and its description from the spool, once the gateway has dealt with it."""
    for path in spool_dir().glob(f"{digest}.*"):
        try:
            path.unlink()
        except OSError:
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hand an image to the chat gateway to post in this conversation.", allow_abbrev=False
    )
    parser.add_argument("file", help="the image: PNG, JPEG, GIF or WebP")
    parser.add_argument("--title", required=True, help="caption: what it shows (host, metric, window)")
    parser.add_argument("--site", help="site repository (default: SRE_HARNESS_SITE or the pointer file)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def action() -> int:
        _root, doc = sre_common.load_site(args.site)
        result = share(Path(args.file), args.title, doc)
        print(json.dumps(result, sort_keys=True))
        if not sre_common.from_chat():
            print("note: only the chat gateway posts shared images; outside it this only checked the file.")
        return 0

    return sre_common.guarded(action)


if __name__ == "__main__":
    sys.exit(main())
