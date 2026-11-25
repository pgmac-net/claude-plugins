"""Clear local incident state once its audit log is provably preserved.

An incident leaves files under the user state directory: its audit spool, a
'.closed' marker, a lock, its state, session files that point at it, and bind
tokens. This module removes them, but only for an incident whose record is
safe somewhere else.

An incident is cleared only when all of these hold:

1. Its '.closed' marker exists: the audit log was committed.
2. It is resolved locally.
3. The same bytes are on the audit repository's fetched default branch, so the
   pull request has merged. The check is the file itself, not a signal that
   something merged.
4. A 'done' signal: its tracking issue is closed, or, where the caller cannot
   ask the code host, the log was committed long enough ago.

Anything else is kept and reported. An incident whose log was never committed
is the only copy of its record: it is reported and never deleted.

Deletion is never recursive and never follows a symlink. Only files whose
names match the incident id pattern, inside the state directory, are removed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess  # noqa: S404 - fixed argv, no shell
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import audit
import audit_hook
import sre_common
from sre_common import INCIDENT_ID, SiteError

DAY = 86400
GIT_TIMEOUT = 30
GH_TIMEOUT = 15


@dataclass
class Policy:
    """What a caller requires before an incident may be cleared."""

    require_issue_closed: bool = True
    min_age_days: int | None = None
    fetch: bool = True
    token_days: int = 7


def _names(directory: Path, suffix: str) -> set[str]:
    """Incident ids that have a file with this suffix in a state subdirectory."""
    if not directory.is_dir():
        return set()
    return {p.stem for p in directory.iterdir() if p.suffix == suffix and INCIDENT_ID.fullmatch(p.stem)}


def local_incidents(state: Path) -> set[str]:
    """Every incident id that has any local state."""
    found = _names(state / "audit", ".jsonl") | _names(state / "audit", ".closed") | _names(state / "audit", ".lock")
    return found | _names(state / "incidents", ".json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def files_of(state: Path, incident_id: str) -> list[Path]:
    """The local files that belong to one incident."""
    found = [state / "audit" / f"{incident_id}{suffix}" for suffix in (".jsonl", ".closed", ".lock")]
    found.append(state / "incidents" / f"{incident_id}.json")
    # Images the gateway posted (ADR 0027): committed beside the log, so they go when it is preserved.
    images = state / "images" / incident_id
    if images.is_dir() and not images.is_symlink():
        found += sorted(images.iterdir())
    sessions = state / "sessions"
    if sessions.is_dir():
        found += [p for p in sessions.glob("*.json") if _read_json(p).get("incident") == incident_id]
    binds = state / "binds"
    if binds.is_dir():
        for token in binds.iterdir():
            try:
                if token.is_file() and token.read_text(encoding="utf-8").strip() == incident_id:
                    found.append(token)
            except OSError:
                continue
    pointer = state / "current"
    if pointer.is_file() and pointer.read_text(encoding="utf-8").strip() == incident_id:
        found.append(pointer)
    return [p for p in found if p.exists() or p.is_symlink()]


def _git_bytes(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, bytes]:
    git = shutil.which("git")
    if not git:
        return 127, b""
    try:
        done = subprocess.run([git, "-C", str(cwd), *args], capture_output=True, check=False, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired:
        return 124, b""
    return done.returncode, done.stdout


def default_ref(checkout: Path) -> str | None:
    """The remote default branch as a ref, from origin/HEAD or the usual names."""
    code, out = _git_bytes(checkout, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    candidates = [out.decode().strip()] if code == 0 and out.strip() else []
    candidates += ["refs/remotes/origin/main", "refs/remotes/origin/master"]
    for ref in candidates:
        if _git_bytes(checkout, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
            return ref
    return None


def preserved(incident_id: str, doc: dict[str, Any], site_root: Path, fetched: set[Path], policy: Policy) -> str | None:
    """None when the spool is byte-identical to the file on the audit repository's default branch, else why not."""
    try:
        checkout = audit.audit_checkout(doc, site_root)
    except SiteError as exc:
        return str(exc)
    if policy.fetch and checkout not in fetched:
        fetched.add(checkout)
        _git_bytes(checkout, "fetch", "--quiet", "origin")
    ref = default_ref(checkout)
    if ref is None:
        return "the audit repository has no fetched default branch to compare with"
    path = f"{doc['audit']['path']}/{incident_id}.jsonl"
    code, remote = _git_bytes(checkout, "show", f"{ref}:{path}")
    if code != 0:
        branch = ref.removeprefix("refs/remotes/")
        return f"{path} is not on {branch} yet: the audit pull request has not merged, or has not been fetched"
    if remote != audit_hook.spool_path(incident_id).read_bytes():
        return f"{path} on {ref.removeprefix('refs/remotes/')} differs from the local spool"
    return None


def merged_log(incident_id: str, doc: dict[str, Any], site_root: Path, fetch: bool = True) -> tuple[bytes | None, str]:
    """(bytes of the incident's log on the audit repository's default branch or None, branch name or reason)."""
    try:
        checkout = audit.audit_checkout(doc, site_root)
    except SiteError as exc:
        return None, str(exc)
    if fetch:
        _git_bytes(checkout, "fetch", "--quiet", "origin")
    ref = default_ref(checkout)
    if ref is None:
        return None, "the audit repository has no fetched default branch to compare with"
    code, data = _git_bytes(checkout, "show", f"{ref}:{doc['audit']['path']}/{incident_id}.jsonl")
    return (data if code == 0 else None), ref.removeprefix("refs/remotes/")


def retire(incident_id: str, log: bytes, how: str) -> None:
    """Leave the tombstone that keeps a committed incident closed once its local files are gone (homelabia#281).

    It names the committed log's length and head so a later look can tell which log was retired. It is
    never removed by gc; only 'attach --reopen' lifts it.
    """
    lines = [line for line in log.decode("utf-8").splitlines() if line.strip()]
    path = audit_hook.retired_path(incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tombstone = {
        "incident": incident_id,
        "records": len(lines),
        "head": hashlib.sha256(lines[-1].encode()).hexdigest() if lines else audit_hook.GENESIS,
        "retired_at": sre_common.iso_utc(sre_common.utc_now()),
        "by": how,
    }
    path.write_text(json.dumps(tombstone, sort_keys=True) + "\n", encoding="utf-8")


def issue_state(incident_id: str, doc: dict[str, Any]) -> str:
    """OPEN or CLOSED. Raises SiteError when it cannot be found out."""
    repo, number = sre_common.split_incident_id(incident_id, doc)
    out = sre_common.run_gh(
        ["issue", "view", str(number), "--repo", repo, "--json", "state", "--jq", ".state"], timeout=GH_TIMEOUT
    )
    return out.strip().upper()


def _data_blocker(
    incident_id: str, doc: dict[str, Any], site_root: Path, fetched: set[Path], policy: Policy
) -> str | None:
    """Why the incident's record is not yet safe elsewhere, or None."""
    state = audit_hook.state_dir()
    spool = audit_hook.spool_path(incident_id)
    info = _read_json(state / "incidents" / f"{incident_id}.json")
    if spool.is_file() and audit_hook.retired_path(incident_id).exists():
        return (
            "a spool started after its log was committed (homelabia#281); a person may drop it with "
            f"incident.py gc --incident {incident_id} --discard-spool"
        )
    if spool.is_file() and not spool.with_suffix(".closed").exists():
        return (
            "its audit log was never committed (no .closed marker): this is the only copy. "
            "Run audit.py commit, or keep it"
        )
    if info.get("status", "resolved") != "resolved":
        return f"the incident is {info.get('status')}, not resolved"
    return preserved(incident_id, doc, site_root, fetched, policy) if spool.is_file() else None


def _done_blocker(incident_id: str, doc: dict[str, Any], policy: Policy) -> str | None:
    """Why the incident is not yet 'done', or None. Issue state where asked, else age."""
    if policy.require_issue_closed:
        try:
            current = issue_state(incident_id, doc)
        except SiteError as exc:
            return f"could not confirm the tracking issue is closed: {exc}"
        return None if current == "CLOSED" else f"the tracking issue is still {current.lower()}"
    if policy.min_age_days is None:
        return None
    state = audit_hook.state_dir()
    closed = audit_hook.spool_path(incident_id).with_suffix(".closed")
    marker = closed if closed.exists() else state / "incidents" / f"{incident_id}.json"
    age = (time.time() - marker.stat().st_mtime) / DAY if marker.exists() else 0.0
    return None if age >= policy.min_age_days else f"committed {age:.0f} day(s) ago; waiting for {policy.min_age_days}"


def assess(
    incident_id: str, doc: dict[str, Any], site_root: Path, fetched: set[Path], policy: Policy
) -> dict[str, Any]:
    """Decide one incident. 'clear' is True only when every required check passes."""
    verdict: dict[str, Any] = {
        "id": incident_id,
        "clear": False,
        "files": [str(p) for p in files_of(audit_hook.state_dir(), incident_id)],
    }
    reason = _data_blocker(incident_id, doc, site_root, fetched, policy) or _done_blocker(incident_id, doc, policy)
    if reason:
        verdict["reason"] = reason
    else:
        verdict["clear"] = True
    return verdict


def stale_tokens(state: Path, exclude: set[str], days: int) -> list[Path]:
    """Bind tokens nobody consumed, older than a number of days."""
    binds = state / "binds"
    if not binds.is_dir():
        return []
    cutoff = time.time() - days * DAY
    return [
        p
        for p in binds.iterdir()
        if p.is_file() and not p.is_symlink() and str(p) not in exclude and p.stat().st_mtime < cutoff
    ]


def _remove(path: Path, state: Path) -> bool:
    """Delete one file, only if it sits inside the state directory. Never recursive."""
    try:
        if path.is_dir() and not path.is_symlink():
            return False
        if state.resolve() not in path.parent.resolve().parents and path.parent.resolve() != state.resolve():
            return False
        path.unlink()
    except OSError:
        return False
    return True


def run(
    doc: dict[str, Any], site_root: Path, policy: Policy, apply: bool, only: list[str] | None = None
) -> dict[str, Any]:
    """Assess local incidents and, with apply, clear those that pass.

    Every local incident is assessed, unless 'only' names some. A named incident
    with no local state is reported as kept, so a mistyped id is visible. A
    targeted run leaves every other incident's files, and the unused bind
    tokens, alone.
    """
    state = audit_hook.state_dir()
    fetched: set[Path] = set()
    local = local_incidents(state)
    wanted = local if only is None else local & set(only)
    verdicts = [assess(i, doc, site_root, fetched, policy) for i in sorted(wanted)]
    if only is not None:
        verdicts += [
            {"id": i, "clear": False, "files": [], "reason": "no local state"} for i in sorted(set(only) - local)
        ]
    cleared = [v for v in verdicts if v["clear"]]
    listed = {path for v in cleared for path in v["files"]}
    tokens = stale_tokens(state, listed, policy.token_days) if only is None else []
    deleted = 0
    if apply:
        for incident_id in (v["id"] for v in cleared):
            spool = audit_hook.spool_path(incident_id)
            if spool.is_file():
                # Written before anything is deleted: from here on nothing records into this incident again.
                retire(incident_id, spool.read_bytes(), "gc")
        for path in [*(Path(p) for p in sorted(listed)), *tokens]:
            deleted += _remove(path, state)
        for incident_id in (v["id"] for v in cleared):
            with contextlib.suppress(OSError):
                (state / "images" / incident_id).rmdir()  # only once empty: never recursive
        audit_hook.release_active_marker()
    return {
        "dry_run": not apply,
        "cleared": [v["id"] for v in cleared],
        "kept": [{"id": v["id"], "reason": v["reason"]} for v in verdicts if not v["clear"]],
        "files": sum(len(v["files"]) for v in cleared) + len(tokens),
        "stale_tokens": len(tokens),
        "deleted": deleted,
    }


def auto_clean(doc: dict[str, Any], site_root: Path, min_age_days: int = 14) -> int:
    """Best-effort cleanup for incident start: no network, age instead of issue state, never raises.

    Returns the number of incidents cleared. Whatever goes wrong, opening an
    incident must not be affected.
    """
    try:
        policy = Policy(require_issue_closed=False, min_age_days=min_age_days, fetch=False)
        return len(run(doc, site_root, policy, apply=True)["cleared"])
    except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
        return 0


def _summary(line: str) -> dict[str, Any]:
    """One spool record as a person reads it before it is dropped."""
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        return {"unreadable": line[:200]}
    tool_input = record.get("input") if isinstance(record.get("input"), dict) else {}
    fields = {
        "ts": record.get("ts"),
        "event": record.get("event"),
        "capture": record.get("capture"),
        "tool": record.get("tool"),
        "command": record.get("command") or tool_input.get("command"),
        "approver": record.get("approver"),
    }
    return {key: value for key, value in fields.items() if value is not None}


def dropped(spool: bytes, merged: bytes) -> list[dict[str, Any]]:
    """The spool's records that are not in the merged log: its tail if it extends the log, else all of it."""
    extra = spool[len(merged) :] if spool.startswith(merged) else spool
    return [_summary(line) for line in extra.decode("utf-8", errors="replace").splitlines() if line.strip()]


def merged_blocker(incident_id: str, doc: dict[str, Any], merged: bytes) -> str | None:
    """Why the merged log cannot be trusted as the record, or None: chain intact and every timeline pin matches."""
    entries = [(line, json.loads(line)) for line in merged.decode("utf-8").splitlines() if line.strip()]
    if not audit.verify(entries)["chain_intact"]:
        return "the merged log's hash chain is broken; keep the spool and investigate"
    repo, number = sre_common.split_incident_id(incident_id, doc)
    anchors = audit.check_anchors(
        {"id": incident_id, "repo": repo, "number": number}, entries, doc["incident"].get("responders")
    )
    if anchors["anchor_mismatches"]:
        problems = "; ".join(item["problem"] for item in anchors["anchor_mismatches"])
        return f"the merged log does not match the timeline's pinned hashes ({problems})"
    return None


def discard_spool(
    incident_id: str, doc: dict[str, Any], site_root: Path, policy: Policy, apply: bool
) -> dict[str, Any]:
    """A person's discard of a spool started after the incident's log was committed and merged (homelabia#281).

    Allowed only for a resolved incident whose log is on the audit repository's default branch with an
    intact chain that matches every hash pinned in its timeline. Lists every record that would go; with
    apply, retires the incident and deletes its local state. The merged log is never touched.
    """
    state = audit_hook.state_dir()
    spool_file = audit_hook.spool_path(incident_id)
    result: dict[str, Any] = {"id": incident_id, "dry_run": not apply, "discarded": False}
    info = _read_json(state / "incidents" / f"{incident_id}.json")
    if info.get("status", "resolved") != "resolved":
        result["reason"] = f"the incident is {info.get('status')}, not resolved: its spool is live, not left over"
        return result
    if not spool_file.is_file():
        result["reason"] = "there is no local spool to discard"
        return result
    merged, where = merged_log(incident_id, doc, site_root, policy.fetch)
    if merged is None:
        result["reason"] = f"the committed log is not on the audit repository's default branch ({where}); keep it"
        return result
    try:
        blocker = merged_blocker(incident_id, doc, merged)
    except (SiteError, ValueError) as exc:
        blocker = f"could not check the merged log: {exc}"
    if blocker:
        result["reason"] = blocker
        return result
    result["merged"] = {"branch": where, "records": len(merged.splitlines())}
    result["drop"] = dropped(spool_file.read_bytes(), merged)
    result["files"] = [str(p) for p in files_of(state, incident_id)]
    if apply:
        retire(incident_id, merged, "gc --discard-spool")
        result["deleted"] = sum(_remove(Path(p), state) for p in result["files"])
        with contextlib.suppress(OSError):
            (state / "images" / incident_id).rmdir()
        audit_hook.release_active_marker()
        result["discarded"] = True
    return result
