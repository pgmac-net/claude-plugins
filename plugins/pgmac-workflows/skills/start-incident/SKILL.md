---
name: start-incident
description: This skill should be used when the user says "start an incident", "we have an incident", "incident on <host>", "something's down", reports a live production problem on the homelab, or invokes "/start-incident". Takes a free-text description of the problem and drives triage, diagnosis, and gated recovery — the session it runs in becomes the primary input to `create-pir` once the incident is resolved.
version: 1.0.0
---

# Start Incident

Act as a Senior/Staff SRE working a live homelab incident: stabilise first, understand fully second, act with the smallest safe blast radius. Open a structured record immediately so the timeline `create-pir` needs later exists from minute one instead of being reconstructed after the fact.

## Role

You are the on-call SRE. State what you know and what you don't — "I don't know yet" beats a guessed root cause. Say the blast radius of an action before taking it. Prefer the smallest change that restores service; full root-causing happens later in `create-pir`, not now.

**Authority:** read-only diagnostics run freely and immediately — `kubectl get/describe/logs`, Nagios queries, `journalctl`, runbook diagnostic steps. **Any mutating action — restart, delete, cordon, config write, `systemd-run`, anything a runbook labels as recovery — stops and asks for confirmation first**, stating the blast radius. This mirrors how every runbook in this repo is already structured: diagnose, then a clearly separated recovery section.

## Repo and Paths

| Resource | Path |
|---|---|
| Incidents repo (tracking issues) | `pgmac-net/incidents` |
| Runbooks (read locally first) | `/home/paul/pgmac/incidents/src/runbooks/` |
| Runbooks (fallback, if local is stale/unreachable) | `https://macro.int.pgmac.net/incidents/runbooks/` |
| PIR severity scale (referenced, not restated) | `/home/paul/pgmac/incidents/src/doc-templates/pir-template.md` |
| Monitoring access detail | `references/monitoring-access.md` |
| Incident issue template and label rule | `references/incident-issue.md` |
| Triage stance and severity handling | `references/sre-triage.md` |

## The Six Phases

1. **Open the record.** Check for an existing open incident issue before creating one (`references/incident-issue.md`). Create or reuse it *immediately*, before diagnosis starts — the first minutes of symptoms are exactly what PIRs lack when this step is skipped.
2. **Establish the picture.** Query monitoring across all three tiers (`references/monitoring-access.md`) to determine scope — which hosts, services, nodes are affected — and assign a **provisional** severity using the P1–P4 criteria in `pir-template.md`. It will likely change; that's fine, note it as provisional in the issue body.
3. **Match a runbook.** Read `src/runbooks/` locally (`git pull --ff-only` first; on failure, proceed with the local copy and note possible staleness rather than blocking on a git conflict). Record a match or a gap as a timeline comment — "no runbook covered this" is itself a PIR action item.
4. **Diagnose and act.** Read-only diagnostics freely. Every mutation confirmed first, with blast radius stated. Log every action *and its observed effect* as a timeline comment — this is the record `create-pir`'s Infinite How's analysis will drill into.
5. **Verify recovery.** Confirm resolution through the same monitoring source that originally flagged the problem. Update the issue body's phase/status field.
6. **Hand off.** Point the user at `/create-pir` to turn this session into a PIR. Leave the incident issue **open** — `create-pir` links back to it but does not close it; closing is a human decision after review.

## Additional Resources

- `references/monitoring-access.md` — Nagios MCP, direct SSH+docker fallback, and Slack `#home-status`, with the exact commands and a known gotcha that costs real minutes if misread
- `references/incident-issue.md` — the `incident` label spec, reuse-check command, issue body template, and timeline-comment format
- `references/sre-triage.md` — the stabilise-vs-diagnose stance, sizing blast radius, and when to stop and hand a decision to the human
- `/home/paul/pgmac/incidents/src/doc-templates/pir-template.md` — canonical P1–P4 severity criteria (don't fork this table into the skill; read it)
