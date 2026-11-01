# pickup-ticket: state labels (claude-plugins#13)

`pgmac-net/claude-plugins#13` asked for `pickup-ticket` to be able to create GitHub labels that don't exist, starting with `in-progress` but not limited to it. Shipped as `pickup-ticket` v2.4.0 and `pgmac-workflows` 1.7.0.

## Problem

Phase 1 said: apply an `in-progress` label if one exists, otherwise skip and never invent labels. On a repo with no such label the ticket got no state marker at all. Nothing in Claude Code permissions blocked label creation (no `gh` allowlist exists); the only barrier was the skill's own rule.

## What changed

- New `skills/pickup-ticket/references/state-labels.md`: the spec, the name-resolution order, the create command and the swap command.
- `SKILL.md`: Phase 1 resolves/creates/applies `in-progress`; Phase 4 swaps to `blocked` and back; Phase 5 swaps to `in-review` after the PR is raised.
- Wording fixes in `start-incident/references/incident-issue.md` and `create-pir/references/github-issues-setup.md`, which called `incident` "the one exception" to the no-invented-labels rule.
- README row for `pickup-ticket`; plugin version 1.6.0 to 1.7.0 in `plugin.json` and `marketplace.json`.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Scope | Fixed vocabulary: `in-progress`, `in-review`, `blocked` | Same pattern as `incident`; a general licence lets label sets drift across repos |
| Naming | Exact or equivalent existing label first; else `status:<state>` if the repo has any `status:*` label, else plain `<state>` | homelabia uses `status:in-progress`, nagios-android uses plain `in-progress`; avoids mixed schemes inside one repo |
| Transitions | One state at a time, swapped | Ticket shows only its current state; filters stay clean |
| Colours | Fixed per state regardless of prefix: `in-progress` `#61deb3`, `in-review` `#0075ca`, `blocked` `#d93f0b` | States tell apart at a glance. `#b60205` is already `incident`. Existing labels are never recoloured |
| Failure | Warn and continue if creation fails | A missing label must not block ticket work |
| Done | Skill never applies a done label or closes the ticket | The user closes tickets after review |

## Verified

Dogfooded on this ticket: `in-progress` was created in `pgmac-net/claude-plugins` (no `status:*` labels, so plain name) and applied. Creating it a second time returns `label with name "in-progress" already exists; use --force ...`, which the skill treats as a match.

## Deviations from plan

One doc correction. The plan assumed `--remove-label` on an absent label is harmless. It is not: `gh issue edit --remove-label blocked` on a repo with no `blocked` label failed with `'blocked' not found`. The reference now says to remove only a state label actually on the ticket, checked via `gh issue view --json labels`.

## Not done

Bulk-creating labels across repos, and recolouring the existing gray `status:*` labels.
