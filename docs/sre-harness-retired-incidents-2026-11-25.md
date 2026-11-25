# sre-harness 0.28.0: committed audit logs stay committed after gc

Ticket: pgmac-net/homelabia#281 · Branch: `281-retired-incidents` · ADR: `plugins/sre-harness/docs/adr/0028-a-committed-audit-log-stays-committed-and-is-only-ever-extended.md`

## Problem

After `audit.py commit` and `incident.py gc --apply`, an incident could start a new audit spool:

- **catalog-66** (2026-11-20, again 2026-11-21): after PR #67 merged and gc cleared it, later approvals in the same conversation re-created `audit/catalog-66.jsonl` (2 records).
- **catalog-28**: the log (31 records) merged in #29. A re-attach made an 11-record spool. Commit failed on the pinned hashes with wording that read as data loss. `commit --offline` opened #68, which would have overwritten main's log; it was closed unmerged.

## Root cause

The only sign that a log was committed was `audit/<id>.closed`, and gc deleted it. Every writer (the hook, `audit.py record`, the gateway's chat decisions) checks `audit_hook.is_closed()`, so after gc they all treated the incident as uncommitted and started a new genesis chain. `attach` of a resolved incident with no `.closed` marker issued a fresh bind token. In `audit.py commit`, `_already_committed` only compared the spool with main for equality. When main's log differed and the audit branch had been deleted after merge, it fell through to a normal commit from main, which replaced the merged file.

A second bug turned up while reading the code: `attach --reopen` before gc left `.closed` in place, so the hook recorded nothing after a reopen.

## What changed

| Want (ticket) | Change |
|---|---|
| 1. Stop spooling after commit + gc | gc writes a tombstone `retired/<id>.json` (records, head, when, by) before deleting. `is_closed()` = `.closed` **or** tombstone. Every existing writer refuses, and the refusal points at `attach --reopen`. |
| 2. Commit never overwrites | If `<id>.jsonl` is on the default branch, the spool must start with exactly those bytes, else refuse. `--offline` does not skip this. |
| 3. Honest mismatch wording | `check_anchors` says `a new chain started at <ts> (<n> records), after the timeline pinned <m> records at <at>` when a mismatching log's genesis record is later than the pin. A rewrite that keeps the original first record still gets the plain mismatch. |
| 4. Person-only discard | `incident.py gc --incident <id> --discard-spool [--apply]`: exactly one incident, resolved, merged log on the default branch with an intact chain that matches every pin. The dry run lists every record it would drop. Refused from chat (in the script, plus a chat-policy deny rule `incident.py gc *--di*`). |
| (extra) Reopen continues | `attach --reopen` lifts `.closed`/tombstone, restores the spool from main if gc removed it, and appends a `reopened` record naming the continued length and head. A later commit is an append. |
| (extra) Backfill | `attach` of a resolved incident with no local state whose log is on main writes the tombstone and issues no token. No migration step. |

## Decisions (grilled 2026-11-25)

1. Tombstone in `state/retired/`, kept indefinitely. Not chosen: keeping `.closed` (gc would keep re-assessing), or asking git from the hook (the hook must stay offline and fast).
2. Post-commit writes are refused; reopen continues the log.
3. Continuation seeds the spool from the merged log, so there is one chain. Not chosen: a fresh spool with a link record.
4. Discarded records are deleted after printing, not kept.
5. Backfill happens through `attach`, not a separate command.

## Deviations from the plan

- The new-chain wording applies only to a pin that already mismatches. The first version also flagged matching pins whose timestamps came before the records, which a test caught.
- gc's reason for a stray spool beside a tombstone now points at `--discard-spool` instead of the misleading "never committed, only copy".

## Verification

- Plugin: ruff check + format clean, pylint exit 0, 1036 tests passed. Covered: hook, chat approval and `audit.py record` refused after gc; reopen before and after gc continues the chain; commit refuses a non-prefix spool online and `--offline`, and commits a prefix as an append; new-chain versus plain wording; discard dry run, apply, chat refusal, unmerged, live, pin mismatch and pin match; attach backfill.
- Gateway: 250 tests passed, pylint exit 0 (no gateway code changed; it uses `is_closed`).
- `srectl completion --check` passes (spec regenerated).

## Rollout

1. Merge the plugin PR.
2. Bump `SRE_HARNESS_REF` in sre-catalog (3 workflows) and the ansible pin, then deploy to macro.
3. On macro: `incident.py gc` (dry run) to list any stray post-commit spools, and `gc --incident <id> --discard-spool` for each after review.
