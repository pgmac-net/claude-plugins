# sre-harness 0.19.0: attach, gc and commit stop undoing a close

Ticket: pgmac-net/homelabia#266 · Branch: `266-incident-attach-gc-commit`

## Problem

Closing out incidents-104 on 19 Nov 2026 went wrong in three ways:

1. `incident.py attach` set `status: open` and dropped `resolved_at`, even when the issue body said `Phase: Resolved`. The Resolved time moved from about 20:08 to 20:16 AEST once `close` was re-run.
2. `incident.py gc` could only act on every incident at once.
3. Afterwards a chat channel bound to incidents-102 refused every non-pre-approved command with `Refused: the approval could not be recorded.`

0.18.0 (homelabia#273) had already fixed part of (1) for incidents that still have local state, and made `--incident` work after the subcommand in `audit.py`, `timeline.py` and `autonomy.py`.

## Finding: it was not a bind-token mismatch

The ticket suspected the gateway and the agent session held different bind tokens. They do not matter here: the gateway never reads bind tokens. It records approvals against `conv.incident`, which it sets from the agent's `incident.py open/attach/promote` output. Bind tokens are only for the audit hook.

The refusal came from `audit.py record_chat_decision`, which raises when the incident's log is committed and closed. sre-catalog#53 committed **incidents-102's** log at 19 Nov 10:10Z, two minutes before incidents-104's (#54), while 102 was still open. The likely route was an `audit.py commit` resolved through the shared current-incident pointer, which 0.18.0 closed for chat. `gc` already leaves other incidents' bindings alone.

## What changed

- `scripts/incident.py` `attach`: with no local state and a body whose Phase is `Resolved`, the incident is attached as resolved and `resolved_at` is read from the UTC part of the body's `Resolved:` line (a stderr note when the line is missing). If its audit log is already committed (`.closed`), no capture starts: no bind token (`"bind": null`) and no `active` marker. `--reopen` is unchanged.
- `scripts/incident.py` `gc --incident <id>` (repeatable) and `state_gc.run(only=...)`: only the named incidents are assessed. An id with no local state is listed under `kept` as `no local state`. The unused-token sweep is skipped. Default behaviour is unchanged.
- `scripts/audit.py` `commit`: refuses an incident that is not resolved, with the `close` command in the message. `--unresolved` overrides it; it is refused in code under `SRE_HARNESS_NO_CURRENT` (any chat session) and by the chat deny rule `*--unr*`.
- Gateway `approvals.py`: when a decision cannot be written, the refusal to the agent and the card now carry the scrubbed, 300-character-bounded reason.
- Docs: README, `start-incident/references/audit-log.md` and the gateway README. They now say which incident the gateway follows and how to check it.
- Versions: plugin 0.19.0, gateway 0.14.0, marketplace 1.30.0.

## Decisions (from the grilling session)

1. **Drop "re-sync bind tokens"**, since tokens are not the cause. Replace it with the commit guard, a reason in the refusal, and docs. The override flag is denied from chat.
2. **Attach to a resolved incident with no local state:** keep it resolved, take the time from the body, and start no capture when the log is closed. When the log is still open it binds as before, so follow-up work and the `commit` call itself are recorded.
3. **Targeted gc:** repeatable, missing id reported rather than an error, no token sweep, no new chat rule (the existing `gc` allow rule covers it).
4. **Flag `--unresolved`, release 0.19.0** with an ansible pin PR.

## Tests

New: attach from a resolved body (with and without the `Resolved:` line), closed log starts no capture, open log still binds; targeted gc (single, repeated, unknown id, other incidents' tokens untouched); commit refused when open, `--unresolved` allowed outside chat and refused in chat; deny rule; refusal reason scrubbed and bounded. Two existing tests that committed an open incident now close it first.

Verified locally: 789 plugin tests, 148 gateway tests, ruff, ruff format, pylint (exit 0, plugin and gateway), template validation, identifier scan.

## Deviations from the plan

- Worked in a worktree of claude-plugins created with `git worktree add`, because the session's own worktree belongs to the ansible repo and `EnterWorktree` would not switch repositories.

## Follow-ups

- The ansible deploy PR pins a commit SHA of claude-plugins (`sre_gateway_plugin_version`); raise it after this PR merges, using the merge commit.
- `/sse` removal on the Nagios MCP remains homelabia#278.
