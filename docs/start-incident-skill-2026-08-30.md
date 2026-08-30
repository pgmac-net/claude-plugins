# start-incident: live incident triage skill + create-pir handoff

**Ticket:** [pgmac-net/incidents#63](https://github.com/pgmac-net/incidents/issues/63)
**PR:** [pgmac-net/claude-plugins#12](https://github.com/pgmac-net/claude-plugins/pull/12)
**Branch:** `63-start-incident-skill`
**Notion:** [start-incident: live incident triage skill + create-pir handoff](https://app.notion.com/p/3cc524b4a0778197b1f2cf1608e7c981)

## What changed

The incidents repo had no structured way to work a *live* homelab incident — `create-pir` only runs after the fact, mining the conversation for a timeline that was never captured in real time. This ticket added a new `start-incident` skill that opens a GitHub Issue as the incident's hold-all record the moment triage begins, and wired `create-pir` to read that issue as its primary source once the incident is resolved.

### `start-incident` (new skill)

Four files under `plugins/pgmac-workflows/skills/start-incident/`:

- `SKILL.md` — Senior/Staff SRE framing, six phases (open the record → establish the picture → match a runbook → diagnose and act → verify recovery → hand off), and the authority rule: read-only diagnostics run freely, every mutating action is gated on explicit confirmation with blast radius stated.
- `references/incident-issue.md` — the `incident` label spec, the reuse-check command (query open `incident`-labelled issues before creating a new one), the issue body template (stable facts, edited in place) and timeline-comment format (AEST-timestamped, append-only).
- `references/monitoring-access.md` — three-tier access: Nagios MCP first, `ssh macro 'docker exec nagios4 ...'` as a fallback, Slack `#home-status` read *always* (not just last-resort) because it carries Wazuh alerts Nagios never sees. Documents the `-32602` gotcha explicitly — that error means the MCP's SSE session needs reconnecting, not that Nagios is down, and the skill is written to not fall through to SSH on it.
- `references/sre-triage.md` — the stance: stabilise before root-causing, how to size blast radius in a confirmation prompt, when to stop and hand a decision to the human, and why comms stay read-only (single-operator homelab).

### `create-pir` (updated)

- Step 1 now reads the incident issue (`gh issue view --comments`) as the primary extraction source when one exists, falling back to conversation-only as before.
- Step 6 applies the `incident` label to every action item, creating the label first if the target repo doesn't have it — the one deliberate, spec'd exception to the pre-existing "never invent a label" rule.
- New Step 10 posts a comment linking the PIR, PR, and every action-item issue back onto the incident issue — and does **not** close it. Closing stays a human decision, matching how `pickup-ticket` already treats issue closure.

## Process

Ran through `pickup-ticket` end-to-end. The ticket lived in `pgmac-net/incidents` but all the actual work landed in `pgmac-net/claude-plugins` — the ticket described a skill to build, not a document to write — so Phase 1 inspected both repos before grilling started.

Planning ran on Opus 5 (Fable 5 wasn't picked), grilling covered nine decisions before the plan was posted to the ticket, and implementation ran on Sonnet per the plan's STANDARD rating.

## Decisions made during grilling

1. **Gated mutations, not full autonomy.** Read-only diagnostics run without asking; anything that changes state stops for confirmation. Matches how every existing runbook in `incidents/src/runbooks/` is already structured — diagnose section, then a clearly separated recovery section.
2. **Issue created immediately, not after triage confirms a real incident.** The first minutes of symptoms are exactly what PIRs lack when this is skipped; a false alarm just costs one closed issue.
3. **Reuse via a query, not an assumption.** `gh issue list --label incident --state open` — zero results creates silently, one or more prompts reuse-or-new. An explicit issue-number argument skips the check.
4. **Templated body + timestamped comments, not a single editable body or free-form narration.** The body holds facts that get edited in place (severity, scope, phase); comments are the append-only timeline `create-pir` Step 1 needs, at the same granularity that step already extracts.
5. **The interesting one — label rule conflict.** `create-pir`'s `github-issues-setup.md` already says "do not invent labels that don't exist on the target repo," for good reason (label sets drift across repos otherwise). The ticket needed `incident` created wherever it's missing. Resolved by making `incident` a *named, spec'd, sole exception* (fixed color `#b60205`, fixed description) rather than loosening the rule generally — every other label still must pre-exist.
6. **Nagios MCP `-32602` documented as a known gotcha, not left for the skill to rediscover mid-incident.** Cross-project memory already had this recorded from a prior investigation; encoding it directly into `monitoring-access.md` means the skill doesn't burn minutes reconnecting the wrong thing during a real incident.
7. **Runbooks read locally first, URL as fallback — not the reverse.** `macro.int.pgmac.net` hosts both Nagios *and* the published incidents site. If `macro` itself is the incident, the URL fallback is dead exactly when it's needed; local `src/runbooks/` isn't.
8. **Comms stay read-only.** The ticket only asked to *read* `#home-status`; a second voice posting into a channel the operator already watches (and that Nagios/Wazuh already post to) adds noise, not signal, in a single-operator homelab.

## A bug found during verification

While testing the install scripts end-to-end (install → uninstall in an isolated `XDG_CONFIG_HOME`), `install-opencode-skills.sh`'s `do_uninstall` silently stopped after removing exactly one symlink. Root cause: `((count++))` on `count=0` evaluates to the *pre-increment* value 0, which bash's arithmetic-context exit status treats as failure — under the script's `set -e`, that aborted the loop after the first iteration even though the increment itself happened correctly. Fixed by switching to `count=$((count + 1))`, matching the pattern already used in the install loop just above it. Re-verified: all 8 skills now install and uninstall cleanly.

## Additional scope: install script drift

Both `install-opencode-skills.sh` and `.ps1` listed only 4 of the 7 skills the README documented (`grill-with-docs`, `grill-me`, `context-engineering` were missing from `SKILL_NAMES`). Flagged in the plan as a deliberate small scope addition rather than a separate issue; approved and fixed in the same PR, plus `start-incident` added to both lists, the README's skill table, and its manual-install/uninstall instructions.

## Deviations from plan

None — implemented as approved, including the in-scope install-script drift fix called out in the plan and confirmed by the user before implementation.
