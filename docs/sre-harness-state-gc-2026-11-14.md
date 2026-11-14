# sre-harness local state cleanup

Ticket: pgmac-net/homelabia#248. Follows the M2 incident core (pgmac-net/homelabia#236).

## The problem

`audit.py commit` copies an incident's audit log to the site's audit repository and
opens a pull request, but leaves the local spool, its `.closed` marker, the incident
state, session files and bind tokens under `~/.local/state/sre-harness/`. Nothing
removed them, and they cannot be removed at commit time: the pull request has not
merged.

## What was built

`sre-harness` v0.7.0:

- **`scripts/state_gc.py`** and **`incident.py gc`** (dry run by default, `--apply` to
  delete).
- **An open-time run**: `incident.py open` clears old, preserved incidents quietly. It
  never uses the network and never fails the open.
- **`templates/site-repo/.githooks/post-merge`**: runs `gc --apply` after a pull.

## Why not just a git hook

A hook is a trigger with gaps. Git never sees an issue close; the hook fires only when a
pull happens on that machine; and `.git/hooks` is not versioned. So the safety is not
in the trigger. It is in the check.

An incident is cleared only when its `.closed` marker exists, it is resolved locally,
the file on the audit repository's fetched default branch is **byte-identical** to the
local spool, and its tracking issue is closed. A missed trigger then leaves harmless
files, and a wrong one cannot delete an unpreserved log. An incident whose log was never
committed is reported and never deleted.

| Trigger | Network | Done signal |
|---|---|---|
| `incident.py gc` | fetches the audit repository, asks the code host | tracking issue closed |
| `post-merge` hook | none (the pull just fetched); asks the code host | tracking issue closed |
| `incident.py open` | none | audit log committed at least 14 days ago |

## Found in review

An automated security review of the first push raised four points, all valid.

| Finding | Change |
|---|---|
| The hook picked the plugin by globbing the plugin cache, which could select the wrong copy | The plugin comes only from `SRE_HARNESS_PLUGIN` or a per-clone `git config`; with neither set the hook does nothing |
| The hook ran `uv` with the repository as its working directory, so a pulled `uv.toml` could influence it | It runs from the plugin directory with `--no-config --no-project` |
| A hook run from a tracked directory executes whatever the last pull brought in | Install by copying into `.git/hooks`; do not use `core.hooksPath`. A test shows a pull changing the tracked copy but not the installed one |
| Removing the `active` marker when no log was open would also have stopped capture for an incident that had just opened and had no log yet. `audit.py commit` had the same flaw | One shared check keeps the marker while any log is open, any incident is not resolved, any state file cannot be read, or any bind token is waiting; both `gc` and `commit` use it |

## Removed, and never

Removed: the spool, `.closed` marker, autonomy lock, incident state, session files that
point at the incident, the `current` pointer if it names it, the `active` marker when no
open log remains, and bind tokens unused for a week.

Never: a directory, anything recursive, anything through a symlink, anything whose name
is not an incident id, anything outside the state directory, and any incident that fails
a check.

## Verification

- 381 tests (27 for the cleanup, 8 for the hook, 7 for the marker); ruff clean; pylint exit code 0.
- Kept cases: never committed, still open, committed but not merged, merged file
  differs, issue open, code host unreachable, no audit checkout, too young.
- The hook is tested under a real `git pull`: it runs with the right arguments and site,
  and a failing `gc`, a missing plugin and a missing `uv` never fail the pull.
- The hook is tested under a real `git pull`: right arguments, site and working directory; plugin
  from config only; a pull cannot change the installed copy.
- Not verified: a post-merge run on the owner's machine; that needs the hook installed in
  `sre-catalog` (a separate change).
