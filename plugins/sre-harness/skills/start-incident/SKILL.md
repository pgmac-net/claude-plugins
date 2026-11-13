---
name: start-incident
description: This skill should be used when the user says "start an incident", "we have an incident", "incident on <host or service>", "something's down", "run an incident drill", reports a live production problem, or asks for a handover or summary of an incident in progress. Takes a free-text description of the problem and drives triage, diagnosis and gated recovery, keeping a typed timeline that the create-pir skill consumes once the incident is resolved.
version: 0.3.0
---

# Start Incident

Act as a senior SRE working a live incident: stabilise first, understand fully second, act with the smallest safe blast radius. Open the record immediately, so the timeline exists from the first minute instead of being reconstructed afterwards.

State what you know and what you don't. "I don't know yet" beats a guessed root cause.

## Before anything else

1. **Load the site**: `../site-check/references/site-config.md`. If no site resolves, stop and say so; do not work an incident against guessed systems.
2. **Read the site's procedures** for the stage you are in. `site.yaml` lists them under `incident.procedures`; the files are in `incident.procedures_path`. They say *how* things are done at this site: monitoring access, fallbacks, evidence capture, comms. This skill says *when*. Where the two disagree on how, the site procedure wins.

`<plugin root>` below is two directories above this skill's base directory.

## Authority

| Tier | What | Rule |
|---|---|---|
| 0 | Read-only diagnostics | Run freely and immediately |
| 1 | Any mutation: restart, delete, cordon, config write, anything a runbook lists under recovery | Stop. State the blast radius (`references/triage-stance.md`). Wait for a clear yes |

A runbook's recovery step is still a mutation. A drill never mutates anything.

## The six phases

1. **Open the record.** Before diagnosis.

   ```bash
   uv run <plugin root>/scripts/incident.py open --title "<what is broken>" --severity <P1-P4> \
     --affected "<hosts or services>" --impact "<what is broken, for whom>"
   ```

   Exit 3 means incidents are already open and lists them: attach to the right one (`incident.py attach <id>`) rather than opening a duplicate, or pass `--new` if this is separate. For a practice run add `--drill`. Record the first symptom as a `discovery` entry.

2. **Establish the picture.** Follow the site's `triage` procedures to query monitoring, then answer: what is affected, since when, how badly. Use the catalog (`references/catalog-in-an-incident.md`) to turn a failing host or check into the services it belongs to, their tier, and what depends on them. Set a **provisional** severity from the site's criteria (`incident.severity_criteria`) and update the body:

   ```bash
   uv run <plugin root>/scripts/incident.py set --severity P2 --phase Diagnosing --affected "…" --impact "…"
   ```

   If the site's `evidence` procedures say some evidence expires, capture it now.

3. **Match a runbook.** Start from the runbooks listed on the affected services' catalog entries, then the runbooks directory. Update your checkout first if the site procedure says how; if you cannot, proceed with the local copy and say it may be stale. Record a match, or the absence of one, as a `decision` entry and in the body (`incident.py set --runbook …`). "No runbook covered this" is itself a finding for the review.

4. **Diagnose and act.** Tier 0 freely; Tier 1 only after confirmation. Record every action **and its observed effect** as an `action` entry: what was changed, who approved it, what happened next. Record findings as `discovery`, choices between options as `decision`.

5. **Verify recovery.** Confirm through the same source that first showed the problem. Capture after-the-fix evidence if the site procedures call for it. Then:

   ```bash
   uv run <plugin root>/scripts/incident.py close
   ```

   This marks the incident resolved. The tracking issue stays open: closing it is a person's decision after review.

6. **Hand off.** Post a `summary` entry, then point the user at the `create-pir` skill.

## The timeline

Every entry goes through one script; never post a timeline comment by hand. Types, when to use each, and the commands: `references/incident-record.md`.

```bash
uv run <plugin root>/scripts/timeline.py add --type discovery --text "…"
```

**Status updates.** Before each wait, and after each phase, ask whether one is owed:

```bash
uv run <plugin root>/scripts/timeline.py due
```

If `due` is true, post an `update` entry now: current state, what changed since the last one, what is next. Nothing runs a timer for you; this check is the timer.

**Handover**, when the user asks or someone else takes over: `timeline.py handover` with its five fields. **Summary**, on request: read `timeline.py export`, then post a `summary` entry in your own words.

## Gotchas

- Opening the record late is the most common failure. The first minutes of symptoms are what reviews most often lack.
- An `action` entry without its observed effect is half an entry. Write it after you have looked.
- Severity is provisional until the review. Change it without ceremony when the picture changes; say why in a `decision` entry.
- A check that is missing from monitoring is not a healthy check. If the catalog lists monitoring for a service and you cannot see it, say so.
- During a drill, prefix nothing by hand: the script marks the record. Do not post drill content to real channels.

## References

- `references/triage-stance.md` — stabilise versus diagnose, sizing blast radius, when to stop and hand a decision to the person
- `references/incident-record.md` — the tracking issue, entry types, handover and summary formats, script usage
- `references/catalog-in-an-incident.md` — using the catalog to find what is affected and what to read
- `../site-check/references/site-config.md` — loading site configuration
