---
name: design-review
description: This skill should be used when the user says "run a design review", "STPA review of <service>", "analyse the control structure of <service>", "find unsafe control actions", "why does <service> keep failing the same way", or when the reliability report lists a service under "Design reviews due". Runs a targeted STPA (System-Theoretic Process Analysis) of one Tier 1 service from evidence, gated by a person at every step, writes the analysis into the site repository and files its findings as issues.
version: 0.1.0
---

# Design review (STPA)

Analyse one service's control structure: who or what controls it, what they can do to it, what they are told about it, and how each of those goes wrong. The output is an analysis file in the site repository and one issue per loss scenario nothing controls today. Method detail: `references/stpa-method.md`. File format: `references/analysis-file.md`.

`<plugin root>` is two directories above this skill's base directory.

**Draft, then gate.** You draft each step from evidence. A person reviews it and corrects it before the next step starts. Never run two steps on one approval, and never mark the analysis `reviewed` yourself until all four gates have passed and every finding is filed.

## Before anything else

1. Load the site: `../site-check/references/site-config.md`. Reliability settings (`reliability:` in `site.yaml`, defaults in `scripts/stpa.py`) say where analyses live.
2. Read the service's catalog entry, its `dependsOn`, its pattern and its runbooks. STPA is targeted: run it for a Tier 1 service, or for a lower tier only when the user asks.
3. If an analysis already exists (`<analyses_path>/<service>.md`), this is a re-review: start from it, take in its `proposed:` entries (from CAST sections of PIRs), and check each existing claim still holds.
4. See why it was asked for: `uv run <plugin root>/scripts/trends.py --json` shows the PIRs attributed to the service, its open action items and why a review is due.

## Gather evidence first

Collect, and keep a list you will cite from:

- **PIRs** attributed to the service (`services:` in their front matter, or matching tags), newest first. Their root-cause chains and timelines are the strongest evidence of how control actually failed.
- **Runbooks** for the service: they describe the human controller's process model.
- **Automation**: every self-heal, event handler, watchdog, controller loop or scheduled job that acts on the service, read from its source (configuration management roles, monitoring configuration, manifests). Note its trigger, its guards and what it reports.
- **Monitoring**: the checks on the service and its Resources, and who each one notifies. Absent checks matter as much as present ones.
- **Open action items** on the service: they are known gaps, and a finding that matches one links it rather than duplicating it.

Read source; do not infer what a script does from its name.

## The four steps, each gated

1. **Losses and hazards.** Two to five losses in the users' or owner's terms (data lost, service unavailable, recovery needs a person at 3 a.m.). Hazards are system states that lead to a loss in the worst case, each linked to its losses. Gate.
2. **Control structure.** Controllers (human, automated, software) with their responsibilities and process model; controlled processes; control actions; feedback, each marked `present`, `inadequate` or `missing`. Draw it (`stpa.py render`) and show the diagram at the gate. Missing feedback found here is already a finding candidate. Gate.
3. **Unsafe control actions.** For every control action, consider all four guidewords: not provided; provided and unsafe; wrong timing or order; stopped too soon or applied too long. Write each UCA with the context that makes it unsafe and the hazards it leads to. Each UCA cites evidence or is marked `hypothesised: true`. A guideword that cannot be unsafe for an action needs no entry; an action with no UCA at all is warned about. Gate.
4. **Loss scenarios.** For each UCA, how it could happen: flawed process model, missing or inadequate feedback, inadequate control algorithm, actuator failure, conflicting controllers, a process disturbance. Mark each scenario `controlled` (name the control that works today, with evidence) or `uncontrolled`. Gate.

After each step: write the file, run `uv run <plugin root>/scripts/stpa.py check`, fix every error, then present the step.

## Findings

Only `uncontrolled` loss scenarios become findings. For each:

1. **Search first**: `gh issue list --repo <repo> --state all --search "<key words>"` in every repository the fix could live in. If an issue already covers it, link that issue and say so; do not file a duplicate.
2. **Otherwise file one issue**, where the site's review procedures say action items go (`incident.procedures.review`), with the site's `design-review` label (create it with the spec those procedures give, never `--force`) and a priority label that already exists. The body links the analysis file path (`analyses/stpa/<service>.md`) and names the scenario id, its UCAs and hazards, and its causal factors, so `trends.py` can attribute it.
3. **Write the issue URL** into the scenario's `finding:`.

Priority is how much the scenario could cost and how likely it is, not how interesting it is. A scenario that has already happened (it has a PIR) outranks one that is hypothesised.

## Finish

1. Set `status: reviewed` and `reviewed:` to today only when all four gates passed and `stpa.py findings <service>` shows no `NOT FILED`.
2. Clear `proposed:` entries you took in; leave any you rejected out, with a sentence in the body saying why.
3. `stpa.py render <service>`, `stpa.py check`, then branch, commit, push and open a pull request in the site repository. Never commit to the default branch.
4. Link the catalog entry to the analysis (`metadata.links`, title "STPA analysis"), in the same pull request.
5. Report: the diagram, the count of UCAs and scenarios, and every finding with its issue.

## Gotchas

- An analysis of "everything" finds nothing. Draw the boundary at one service and the controllers that act on it, and write it in `scope:`.
- Automated controllers are where this pays off: self-heal scripts and watchdogs act with no one watching, on a process model that can be stale. Ask what each one believes, and what tells it it was wrong.
- A human controller's feedback is usually an alert. If a person only learns of a state by being woken by its consequence, that feedback is `missing`.
- Hypothesised is not a failure. It is honest, and it tells the next review where to look for evidence.
- Do not restate PIR root causes as findings. A finding is a control gap; the PIR's action item may already close it.

## References

- `references/stpa-method.md` — the method, the guidewords, causal factors, a worked example
- `references/analysis-file.md` — the analysis file's format and a minimal example
- `../create-pir/references/cast.md` — CAST, the same model applied to one incident
