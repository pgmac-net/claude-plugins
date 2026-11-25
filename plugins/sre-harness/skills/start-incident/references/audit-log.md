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

The hook proves a command ran. It cannot know why it was allowed. A Tier 2 action is run and recorded by `autonomy.py run` (`autonomy.md`). Before every Tier 1 mutation, once the person has said yes, record the approval, giving `--command` exactly as you will run it so the two can be paired:

```bash
uv run <plugin root>/scripts/audit.py record --kind approval \
  --text "<blast radius as stated: target, effect on dependants, rollback>" \
  --command "<the exact command>" --approver "<who said yes>"
```

`record` prints the words it will pair on (`approves: ...`); if they differ from what you will run, fix the `--command` before you run anything. Pairing is per simple command, on its words, so `-p {"a":1}` written plainly and `-p '{"a":1}'` in the shell match. The words must be the same: an extra flag (`--wait`, `--timeout`) makes a different command, so approve the command exactly as you will run it. An approval recorded earlier in the same Bash call as its command counts, but one recorded after it does not. One approval covers one run of one simple command, or of a whole command line if that is what was approved.

Record a refusal the same way with `--kind denial`. An approval record is your statement that the person agreed; it is not proof that they did. Record only what was actually said. Then run the command, and write the `action` timeline entry once you have seen its effect.

**Driven from chat, you record no approvals.** When the session runs through the chat gateway, a command that needs approval stops at a permission prompt; the gateway shows it to an approver, records their decision, bound to that one call, and then answers. State the blast radius in your message before you run the command, so the request can link to it. A refused or expired request comes back as a denial: do not retry the same command without asking again in the channel.

**In an unattended triage** (`unattended-triage.md`), the gateway also writes:
- a `denial` with `outcome: refused` and no approver for every call the triage-only policy refused;
- one `policy_change` (`triage-only` to `chat`, who, and a link to their message) when a responder first speaks.

A promoted incident's log starts with a `promoted_from` record, naming the triage's log and its head hash. `audit.py commit` commits both logs together.

**If your agent has no hooks**, nothing is captured automatically. Record each command yourself, immediately after running it, with `--kind command --command "<exact command>" --text "<outcome>"`. These are marked `self-reported`: a weaker record, and the review will say so.

## Inspecting

```bash
uv run <plugin root>/scripts/audit.py show      # where it is, how many records
uv run <plugin root>/scripts/audit.py verify --check-anchors
#   chain intact? matches the hashes pinned in the timeline? attempts that never completed?
#   anything that touched the state directory? anything secret-shaped left after redaction?
#   any mutating shell command with no approval for that exact command?
```

`unauthorised_mutations` lists each Bash call with an unpaired mutation, and under `segments` the simple commands in it that no approval covers. A simple command is judged by its program, after `sudo`, `timeout`, `env` and similar wrappers. A carrier's payload is judged the same way: ssh's remote command, `sh -c`, `watch`, `xargs`, `find -exec`, `docker exec`, `kubectl exec`. So is a command substitution, and a heredoc fed to a shell. Prose in an argument (`--text`, `--body`, a commit message) is not a command, and changes made on your own machine (`git push`, `sed -i`, `mv`) are listed like any other. A command line that cannot be parsed, often one shortened by redaction, is judged by its whole text.

## Closing

The hook keeps capturing after `incident.py close`, so that the review work is on record too. When the review's pull request is open:

```bash
uv run <plugin root>/scripts/audit.py commit --incident <incident id>
```

`incident.py close` and `dismiss` print the **close-out** steps still to do, worked out from the real state: commit the log, a person merges its pull request, a person closes the tracking issue, then `incident.py gc --apply`. Follow that list.

It verifies the chain, compares the log with the hashes pinned in the timeline, scans it once more for anything secret-shaped, copies the log to the site's private audit location on a branch `audit/<incident id>`, pushes, opens a pull request, and stops capture. It uses a temporary worktree and leaves the checkout as it found it. It refuses to commit a broken chain, a log that does not match its pinned hashes, or a log with a leftover secret. Report any of those to the person; do not work around them, and do not edit the log to make the check pass.

Running it again is safe. If the same log is already on the default branch it says `preserved`; if it is on the audit branch waiting for its pull request it says `pending`; either way it exits 0 and makes sure capture has stopped. If the log has changed since it was pushed, it refuses rather than overwrite an audit record; `--amend` pushes the new log to the same branch as a new commit, and is for a person to decide.

A log already on the default branch is only ever **extended**: the spool must start with exactly the merged bytes (a reopened incident's continuation), or commit refuses with `not overwriting an audit record`. `--offline` does not skip this. When the spool's chain began after a pin in the timeline, the refusal says `a new chain started at <ts> … after the timeline pinned <n> records`: that is a spool started after the log was committed, not lost or tampered records. See "Afterwards: local state" for what to do with it.

`audit.py commit` refuses an incident that is not resolved, because committing stops capture and the gateway then refuses every approval for that incident. Run `incident.py close --incident <id>` first. `--unresolved` overrides this and is for a person to decide.

From the chat gateway, `audit.py commit` and `incident.py gc` run without an approval request, but `commit --offline`, `commit --unresolved` and `gc --ignore-issue-state` are refused. Once the log is committed, the gateway refuses any further approval request for that incident at once, because none could be recorded. If a decision cannot be written for any other reason, the refusal now says why (`Refused: the approval could not be recorded: <reason>`).

**Which incident the gateway follows.** The gateway records approvals against the incident named by the agent's last `incident.py open`, `attach` or `promote` in that channel. Bind tokens are the audit hook's business only; the gateway never reads them, and attaching again from a terminal does not move or break the channel. To see which incident a channel follows, say `status` in it. To move a channel, have its agent run `incident.py attach <id>`. If every non-pre-approved command is refused, check that incident's audit log is not already committed (`audit.py show --incident <id>`).

## Naming the incident

Scripts called without `--incident` act on the machine's *current* incident, the one last opened or attached. Under the chat gateway every session shares that pointer, so the gateway sets `SRE_HARNESS_NO_CURRENT=1` and a call without `--incident` is refused. Pass it after the subcommand, e.g. `timeline.py add --incident <id> ...`, so the call still matches the allow-list. Writes print the incident they touched.

`incident.py attach` to an incident that is already known keeps its local state and only refreshes what the issue says. A resolved incident stays resolved; `attach --reopen` reopens it and records that as a timeline decision. With no local state (cleared by `gc`, or another machine), an issue body whose Phase is `Resolved` is attached as resolved, with the resolved time taken from the body's `Resolved:` line. If its audit log is already committed, no capture starts: the output has `"bind": null` and no `active` marker is written. That holds after `gc` too: a resolved incident with no local state whose log is on the audit repository's default branch is retired on attach.

`attach --reopen` of a committed or retired incident continues its log: if `gc` already removed the spool, it is restored byte for byte from the audit repository's default branch, then a `reopened` record is added that names the committed length and head. A later `audit.py commit` appends to the merged log. If the committed log cannot be read, or a stray spool started after the commit is in the way, reopen refuses.

## Afterwards: local state

`audit.py commit` leaves the local spool in place, because its pull request has not merged. Once the audit pull request is merged and the tracking issue is closed, `incident.py gc --apply` removes it, but only if the file on the audit repository's default branch is byte-identical to the local copy. Run `incident.py gc` (a dry run) to see what would go and why anything is kept. Never delete files under the state directory by hand: `verify` reports tool calls that touch it, and an uncommitted log is the only copy.

`gc --apply` leaves one small file per cleared incident, `retired/<id>.json` (records, head, when). While it exists the incident counts as committed: the hook, `audit.py record` and the gateway record nothing more for it, and refusals point at `attach --reopen`. Only `attach --reopen` removes it.

**A spool started after commit** (left over from before sre-harness 0.28.0, homelabia#281) is dropped only by a person:

```bash
uv run <plugin root>/scripts/incident.py gc --incident <id> --discard-spool          # dry run: lists every record it would drop
uv run <plugin root>/scripts/incident.py gc --incident <id> --discard-spool --apply
```

It needs exactly one `--incident`, a resolved incident, and the committed log on the audit repository's default branch with an intact chain that matches every hash pinned in the timeline. It retires the incident and deletes its local state; the merged log is never touched. It is refused from chat. If the dropped records matter (real approvals), reopen instead and record them again in the continued log.

## Limits to state honestly

- **This is a record kept by a cooperating agent, not a tamper-proof one.** The log and the files that control capture belong to the same user the agent runs as. A process that set out to evade or rewrite them could. The hash chain, the hashes pinned in timeline comments and the state-directory report make accidents and after-the-fact edits visible. Shipping records off the machine as they are written is the control for a hostile process, and is not built yet.
- The hook fails open. If it errors or times out it never blocks your tool call; it writes a `capture_error` record where it can. `verify` reports the count.
- The chain alone cannot show that the last line was altered or that lines were cut from the end. The pinned hashes cover everything up to the last timeline entry; the `head` hash in the commit message covers the rest from commit time on.
- Redaction is pattern-based. Do not put a secret on a command line believing the log will hide it.
