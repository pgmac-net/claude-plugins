# SRE Triage Stance

The judgment calls that don't reduce to a command.

## Stabilise First, Understand Fully Second

The goal during the incident is restoring service, not root-causing it — that's `create-pir`'s job, done calmly afterward with the full session as material. If a runbook's recovery step is likely to work, prefer it over a deeper investigation that delays mitigation. Note *why* a shortcut was taken as a timeline comment; `create-pir` will still find the real root cause later from the diagnostics that were captured along the way.

Don't let this become an excuse to skip diagnosis, though — acting on the wrong runbook match can widen an outage. The read-only diagnostics in Phase 2–3 exist to confirm a match before committing to it, not to be skipped for speed.

## Sizing Blast Radius Before a Mutating Action

Before any restart, delete, cordon, or config write, say — in the confirmation prompt itself, not just internally — three things:

1. What the action targets (one pod? one node? a whole daemonset?)
2. What happens to anything currently depending on that target while the action runs
3. What the rollback is, if the action doesn't help

A runbook that specifies an order (e.g. "restart k8s-dqlite before kubelite," "cordon before draining") is stating a blast-radius constraint, not a suggestion — follow the order even under pressure to move faster.

## Provisional Severity, and When It's Wrong

Assign P1–P4 as soon as scope is roughly known (Phase 2), using the criteria in `pir-template.md` — but expect it to move as the picture clarifies. A single service down that looked like P2 can turn out to be masking a P1 once dependent services are checked. Re-assign in the issue body without ceremony; the provisional value was never meant to be final, and `create-pir` will confirm severity properly at write-up time regardless.

## When to Stop and Hand a Decision to the Human

This skill's gated-mutation rule (SKILL.md) covers the routine case — any single mutating action. Stop and explicitly ask, rather than just confirming the next command, when:

- Two runbooks plausibly match and their recovery paths conflict (different order, or one contraindicates the other)
- The next diagnostic step itself carries risk (e.g., a command that's read-only in principle but known to be expensive/slow on a stressed system)
- Recovery so far hasn't moved the needle after a reasonable attempt, and the next option is materially more invasive than what's been tried
- The incident's scope crosses into something this skill has no runbook or monitoring visibility into at all

In each case, state the options and their trade-offs plainly — this is a homelab with a single operator, so "hand a decision to the human" means putting it to the same person running the session, not paging someone else.

## Comms Stay Read-Only

This skill reads `#home-status` for signal (`monitoring-access.md`) but never posts to it. Nagios and Wazuh already post there; a second voice narrating the same incident adds noise without adding an audience — it's a single-operator homelab, so incident comms is the operator talking to themselves via the GitHub issue, which is also the durable record `create-pir` needs. If that changes (a second person starts watching `#home-status`), revisit this — it's a deliberate choice, not an oversight.
