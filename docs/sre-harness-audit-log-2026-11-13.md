# sre-harness audit log (M2, part 2 of 4)

Ticket: pgmac-net/homelabia#236. Builds on `sre-harness-incident-skills-2026-11-13.md`.

## What was built

`sre-harness` v0.4.0 adds a machine-captured audit log for incidents.

- **`hooks/hooks.json` and `scripts/audit_hook.py`** — a `PreToolUse` and
  `PostToolUse` hook. During an incident it writes one JSON line per tool call
  attempted and completed. Standard library only. It never blocks a tool call
  and never prints.
- **`scripts/audit.py`** — `record` (approvals, denials, decisions, and
  commands for agents without hooks), `show`, `verify`, `commit`.
- **`scripts/redact.py`** — secret detection and redaction, now shared by the
  validator and the audit log.

## How it behaves

| Question | Answer |
|---|---|
| When does it capture? | Only in a session that has run `incident.py open` or `attach`, until `audit.py commit` |
| What does it cost otherwise? | A shell `test` for a marker file; Python is not started |
| What is stored of a tool's input? | All of it, redacted: token shapes, `password=`-style assignments, URL credentials, values of credential environment variables. Long strings truncated |
| What is stored of a tool's output? | Size and SHA-256 only |
| How are denials seen? | An `attempt` with no `complete` |
| How are approvals seen? | An explicit `approval` record, added before the command runs |
| Can edits be noticed? | Each line carries the hash of the line before; each timeline entry pins the head hash and count on the code host; the head hash goes into the commit message. See Limits |
| Where does it live? | Spooled under the user state directory; committed to the site's private audit location on a branch, as a pull request |

## Found in review

An automated security review of the first push raised four points. All were valid.

| Finding | Change |
|---|---|
| A session could be bound to an incident by any command that mentioned the script and printed an id | Binding now needs a random single-use token that `incident.py` writes to the state directory; the incident id is read from that file. Moving a live session to another incident is recorded in the log it leaves |
| Redaction missed `--password value`, `-u user:pass`, Basic headers and file content | Patterns added; file content and free text passed to tools are stored as size and hash only; `commit` scans the log once more and refuses to publish a leftover secret |
| Patterns ran over the whole input before truncation; the log was re-read on every append | Input is cut before any pattern runs; every pattern is bounded; append reads only the last line |
| The marker file and the unkeyed chain are writable by the agent | Not fully fixable on one account. Each timeline entry now pins the log's head hash and count on the code host, `verify` reports tool calls that touch the state directory, and the claims are reworded: this is a record kept by a cooperating agent, not a tamper-proof one |

## Limits

- **Not tamper-proof.** The log and the files that control capture belong to
  the same user as the agent. The chain, the pinned hashes and the
  state-directory report make accidents and later edits visible; they do not
  stop a process that sets out to defeat them. Off-host shipping is the
  control for that and is a later piece of work.
- The hook fails open. On an internal error it writes a `capture_error` record
  where it can; `verify` reports the count.
- Redaction is pattern-based and will miss an unusual secret.
- The chain alone cannot show a change to the last line or a cut from the end;
  the head hash recorded at commit pins both from then on.
- Agents without hooks self-report. Those records are marked and are a weaker
  guarantee.
- The hook payload shape was taken from Claude Code's documented fields and
  has not yet been exercised inside a live Claude Code session; the drill does that.

## Verification

- 159 tests pass; ruff clean; pylint 10.00/10.
- The hook was run through the exact shell command in `hooks.json`: no output
  and no files without the marker; a chained record with it.
- `audit.py commit` tested against a local bare repository: branch pushed,
  file identical to the spool, checkout left clean, capture stopped.
