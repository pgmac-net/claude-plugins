"""Images the agent shares with share.py: where their copies go, and the line the tracking issue gets (ADR 0027).

The gateway posts an image only to the conversation whose agent ran share.py, and keeps copies only once it is
posted: for an incident, in the harness state (audit.py commit takes them beside the log) and the evidence
directory; for a question, beside its log. The issue gets a description, never the image.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sre_gateway import harness
from sre_gateway.chat import ChatAdapter, Place, UploadRefused
from sre_gateway.tracker import Tracker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Destination:
    """The conversation the image belongs to: where it is posted and what record it joins."""

    place: Place
    incident: str | None
    evidence_dir: Path | None
    qlog: Any = None  # questions.QuestionLog, for a question


@dataclass(frozen=True)
class Outcome:
    """What to tell the channel (when the post failed) and what to show in status."""

    say: str | None = None
    problem: str | None = None


async def deliver(  # noqa: PLR0913  pylint: disable=too-many-arguments,too-many-positional-arguments
    chat: ChatAdapter, tracker: Tracker, scrub: Callable[[str], str], image: dict[str, Any], where: Destination
) -> Outcome:
    """Post one spooled image to its conversation, then keep its copies and describe it on the issue.

    The spool entry is removed whatever happens: an image that could not be posted is not retried.
    """
    try:
        return await _deliver(chat, tracker, scrub, image, where)
    finally:
        harness.share.discard(image["sha256"])


async def _deliver(
    chat: ChatAdapter, tracker: Tracker, scrub: Callable[[str], str], image: dict[str, Any], where: Destination
) -> Outcome:
    title = scrub(image["title"])
    try:
        data = Path(image["path"]).read_bytes()
        link = await chat.upload(where.place, data, f"{image['sha256'][:12]}.{image['type']}", title)
    except UploadRefused as exc:
        return Outcome(f"The image could not be posted. {exc}.", f"posting an image failed: {exc}")
    except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
        log.exception("could not post an image")
        name = type(exc).__name__
        return Outcome(f"The image could not be posted ({name}).", f"posting an image failed: {name}")
    problem = None
    for target in copies(image, where.incident, where.evidence_dir, where.qlog):
        try:
            write_private(target, data)
        except OSError as exc:
            log.exception("could not keep a copy of a shared image")
            problem = f"keeping a copy of an image failed: {type(exc).__name__}"
    try:
        if where.qlog is not None:
            where.qlog.shared(image["sha256"], image["type"], len(data), image["title"])
        if where.incident:
            await tracker.comment(where.incident, issue_line(image, title, link))
    except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
        log.exception("could not record a shared image")
        problem = f"recording a shared image failed: {type(exc).__name__}"
    return Outcome(None, problem)


def copies(image: dict[str, Any], incident: str | None, evidence_dir: Path | None, qlog: Any) -> list[Path]:
    """Where the durable copies of one posted image go. None before a record exists and outside a question."""
    name = f"{image['sha256']}.{image['type']}"
    if incident:
        found = [harness.share.incident_images_dir(incident) / name]
        if evidence_dir is not None:
            found.append(evidence_dir / incident / f"shared-{image['sha256'][:12]}.{image['type']}")
        return found
    if qlog is not None:
        return [qlog.path.with_name(f"{qlog.id}.images") / name]
    return []


def write_private(path: Path, data: bytes) -> None:
    """Write a file only its owner can read, in a directory only its owner can enter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def issue_line(image: dict[str, Any], title: str, link: str | None) -> str:
    """The tracking issue's line for a posted image. The caption sits in code, so it cannot mention or link."""
    caption = title.replace("`", "'")
    body = (
        f"**Image shared in chat** (agent): `{caption}` "
        f"({image['type']}, {max(1, image['bytes'] // 1024)} KB, sha256 `{image['sha256'][:16]}`)"
    )
    return body + (f"\n\n[image]({link})" if link else "")
