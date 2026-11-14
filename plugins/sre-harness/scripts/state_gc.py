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


def run(doc: dict[str, Any], site_root: Path, policy: Policy, apply: bool) -> dict[str, Any]:
    """Assess every local incident and, with apply, clear those that pass."""
    state = audit_hook.state_dir()
    fetched: set[Path] = set()
    verdicts = [assess(i, doc, site_root, fetched, policy) for i in sorted(local_incidents(state))]
    cleared = [v for v in verdicts if v["clear"]]
    listed = {path for v in cleared for path in v["files"]}
    tokens = stale_tokens(state, listed, policy.token_days)
    deleted = 0
    if apply:
        for path in [*(Path(p) for p in sorted(listed)), *tokens]:
            deleted += _remove(path, state)
        audit_dir = state / "audit"
        logs = audit_dir.glob("*.jsonl") if audit_dir.is_dir() else []
        open_logs = [p for p in logs if not p.with_suffix(".closed").exists()]
        if not open_logs:
            (state / "active").unlink(missing_ok=True)
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
