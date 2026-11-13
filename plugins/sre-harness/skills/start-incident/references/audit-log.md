# The audit log

Separate from the timeline. The timeline is curated and written for people. The audit log is complete and written by a hook: every tool call attempted and completed while the incident is open, as one JSON line each. You do not write it and cannot skip it.

## What is captured

| Field | Notes |
|---|---|
| `event` | `attempt` before a tool runs, `complete` after. An attempt with no completion was denied or interrupted |
| `tool`, `input` | Inputs are redacted: known token shapes, `password=`-style assignments, credentials in URLs, and the values of environment variables that hold credentials. Very long strings are truncated |
| `output` | **Size and SHA-256 only.** Output content is never stored |
| `capture` | `hook`, or `self-reported` for records added with `audit.py record` |
| `prev` | Hash of the previous line. An edit or deletion breaks the chain |

Capture starts when a session runs `incident.py open` or `attach`, and only for that session. Outside an incident nothing is recorded.

## What you must add

The hook proves a command ran. It cannot know why it was allowed. Before every Tier 1 mutation, once the person has said yes, record the approval:

```bash
uv run <plugin root>/scripts/audit.py record --kind approval \
  --text "<blast radius as stated: target, effect on dependants, rollback>" \
  --command "<the exact command>" --approver "<who said yes>"
```

Record a refusal the same way with `--kind denial`. Then run the command, and write the `action` timeline entry once you have seen its effect.

**If your agent has no hooks**, nothing is captured automatically. Record each command yourself, immediately after running it, with `--kind command --command "<exact command>" --text "<outcome>"`. These are marked `self-reported`: a weaker record, and the review will say so.

## Inspecting

```bash
uv run <plugin root>/scripts/audit.py show      # where it is, how many records
uv run <plugin root>/scripts/audit.py verify    # chain intact? attempts that never completed?
```

## Closing

The hook keeps capturing after `incident.py close`, so that the review work is on record too. When the review's pull request is open:

```bash
uv run <plugin root>/scripts/audit.py commit
```

It verifies the chain, copies the log to the site's private audit location on a branch `audit/<incident id>`, pushes, opens a pull request, and stops capture. It uses a temporary worktree and leaves the checkout as it found it. It refuses to commit a broken chain: report that to the person rather than working around it.

## Limits to state honestly

- The hook fails open. If it errors it never blocks your tool call; it writes a `capture_error` record where it can. `verify` reports the count.
- The chain cannot show that the last line was altered or that lines were cut from the end. The `head` hash written into the commit message pins both at commit time.
- Redaction is pattern-based. Do not put a secret on a command line believing the log will hide it.
