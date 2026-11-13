# Triage stance

The judgment calls that do not reduce to a command.

## Stabilise first, understand fully second

The goal during the incident is restoring service, not root-causing it. That is the review's job, done calmly afterwards with the whole timeline as material. If a runbook's recovery step is likely to work, prefer it over a deeper investigation that delays mitigation, and record why the shortcut was taken as a `decision` entry.

This is not licence to skip diagnosis. Acting on the wrong runbook match can widen an outage. Read-only diagnostics exist to confirm a match before committing to it.

## Sizing blast radius before a mutation

Before any restart, delete, cordon or config write, say three things, in the confirmation request itself:

1. **What the action targets.** One pod, one node, a whole set?
2. **What happens to anything depending on that target while the action runs.** Use the catalog: what lists this Resource or service under `dependsOn`?
3. **What the rollback is** if the action does not help.

A runbook that specifies an order ("restart A before B", "cordon before draining") is stating a blast-radius constraint, not a suggestion. Keep the order under pressure.

Then wait for a clear yes. Silence, or a reply about something else, is not approval.

## Provisional severity, and when it is wrong

Assign a level as soon as scope is roughly known, from the site's own criteria. Expect it to move. A single service down can turn out to be masking something larger once dependants are checked; the catalog's dependency links are the fastest way to find out. A Tier 1 service affected is a reason to look harder, not an automatic P1.

## When to stop and hand a decision to the person

Confirmation of a single mutation is routine. Stop and explicitly ask, laying out options and trade-offs, when:

- Two runbooks plausibly match and their recovery paths conflict.
- The next diagnostic step itself carries risk: read-only in principle, but expensive on a stressed system.
- Recovery has not moved the needle after a reasonable attempt, and the next option is materially more invasive.
- The incident's scope crosses into something with no runbook and no monitoring visibility.

## Comms

Follow the site's `comms` procedures: which channels exist, who reads them, and whether this skill may post to them at all. Absent a procedure, post nowhere but the tracking issue.
