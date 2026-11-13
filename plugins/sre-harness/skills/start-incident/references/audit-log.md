# The audit log

Separate from the timeline. The timeline is curated and written for people. The audit log is written by a hook, not by you: every tool call attempted and completed while the incident is open, as one JSON line each.

**Leave its files alone.** The log, and the files that switch capture on, live under the user state directory (`~/.local/state/sre-harness/`). Never read, edit, move or delete anything there except through the harness scripts. `verify` reports every tool call that touched that directory, and the review will have to explain each one.

## What is captured

| Field | Notes |
|---|---|
| `event` | `attempt` before a tool runs, `complete` after. An attempt with no completion was denied or interrupted |
| `tool`, `input` | Inputs are redacted: known token shapes, `password=` and `--password value` forms, `-u user:pass`, authorization headers, credentials in URLs, and the values of environment variables that hold credentials. Very long strings are truncated. File content and other free text passed to a tool (`content`, `new_string`, `prompt`, …) is stored as size and hash only |
| `output` | **Size and SHA-256 only.** Output content is never stored |
| `capture` | `hook`, or `self-reported` for records added with `audit.py record` |
| `prev` | Hash of the previous line. An edit or deletion breaks the chain |

Capture starts when a session runs `incident.py open` or `attach`, and only for that session: the script prints a single-use bind token, and the hook binds the session that printed it. Outside an incident nothing is recorded. Attaching a session that is already in one incident to another is recorded in the log it leaves.

Each timeline entry you post also pins the log's current head hash and record count in its hidden marker, on the code host. That is what lets a later rewrite of the log be noticed.

## What you must add

The hook proves a command ran. It cannot know why it was allowed. A Tier 2 claim records itself (`autonomy.md`). Before every Tier 1 mutation, once the person has said yes, record the approval, giving `--command` exactly as you will run it so the two can be paired:

```bash
uv run <plugin root>/scripts/audit.py record --kind approval \
  --text "<blast radius as stated: target, effect on dependants, rollback>" \
  --command "<the exact command>" --approver "<who said yes>"
```

Record a refusal the same way with `--kind denial`. An approval record is your statement that the person agreed; it is not proof that they did. Record only what was actually said. Then run the command, and write the `action` timeline entry once you have seen its effect.

**If your agent has no hooks**, nothing is captured automatically. Record each command yourself, immediately after running it, with `--kind command --command "<exact command>" --text "<outcome>"`. These are marked `self-reported`: a weaker record, and the review will say so.

## Inspecting

```bash
uv run <plugin root>/scripts/audit.py show      # where it is, how many records
uv run <plugin root>/scripts/audit.py verify --check-anchors
#   chain intact? matches the hashes pinned in the timeline? attempts that never completed?
#   anything that touched the state directory? anything secret-shaped left after redaction?
#   any mutating command with neither an approval nor a Tier 2 claim?
```

## Closing

The hook keeps capturing after `incident.py close`, so that the review work is on record too. When the review's pull request is open:

```bash
uv run <plugin root>/scripts/audit.py commit
```

It verifies the chain, compares the log with the hashes pinned in the timeline, scans it once more for anything secret-shaped, copies the log to the site's private audit location on a branch `audit/<incident id>`, pushes, opens a pull request, and stops capture. It uses a temporary worktree and leaves the checkout as it found it. It refuses to commit a broken chain, a log that does not match its pinned hashes, or a log with a leftover secret. Report any of those to the person; do not work around them, and do not edit the log to make the check pass.

## Limits to state honestly

- **This is a record kept by a cooperating agent, not a tamper-proof one.** The log and the files that control capture belong to the same user the agent runs as. A process that set out to evade or rewrite them could. The hash chain, the hashes pinned in timeline comments and the state-directory report make accidents and after-the-fact edits visible. Shipping records off the machine as they are written is the control for a hostile process, and is not built yet.
- The hook fails open. If it errors or times out it never blocks your tool call; it writes a `capture_error` record where it can. `verify` reports the count.
- The chain alone cannot show that the last line was altered or that lines were cut from the end. The pinned hashes cover everything up to the last timeline entry; the `head` hash in the commit message covers the rest from commit time on.
- Redaction is pattern-based. Do not put a secret on a command line believing the log will hide it.
