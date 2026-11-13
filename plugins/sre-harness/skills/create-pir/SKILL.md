---
name: create-pir
description: This skill should be used when the user says "create a PIR", "write a post-incident review", "generate an incident report", "document this incident", "write a post-mortem", "capture this incident", or asks for formal incident documentation once an incident is resolved. Writes the review from the incident's timeline, applies Infinite How's root cause analysis, evaluates runbooks, files action items and raises a pull request.
version: 0.5.0
---

# Create a post-incident review (PIR)

Turn a resolved incident into a review document, end to end: analysis, runbooks, tracked action items, pull request. Do the work; do not describe it.

## Before anything else

1. **Load the site**: `../site-check/references/site-config.md`.
2. **Read the site's `review` procedures** (`incident.procedures.review` in `site.yaml`). They define this site's document template, format rules, file naming, where reviews and runbooks live, how the result is validated, and where action items are filed. This skill defines the steps; where the two disagree on format, the site procedure wins.

`<plugin root>` below is two directories above this skill's base directory.

## Sources, in order of trust

1. **The timeline export.** If the incident was worked with `start-incident`, this is the primary source:

   ```bash
   uv run <plugin root>/scripts/incident.py attach <incident id>    # if it is not the current incident
   uv run <plugin root>/scripts/timeline.py export
   ```

2. **The tracking issue body**: final severity, affected scope, matched runbook.
3. **The conversation**: for reasoning the timeline did not capture. Timeline entries record what happened, not always why.

With no tracking issue, extract from the conversation alone and say so in the document.

## The steps

1. **Extract** metadata (dates, duration, severity, resolution, impact, affected services), the timeline, one to four root-cause chains, and action items. Collect evidence gathered during the incident; if the site's `evidence` procedures can still produce something that proves cause or recovery, produce it now, before it expires.
2. **Root-cause** each chain with Infinite How's: `references/infinite-hows.md`.
3. **Name** the document by the site's convention.
4. **Write** it against the site's template. Embed evidence where it proves a claim.
5. **Runbooks**: for each chain, decide create, extend or skip, and act on it.
6. **Catalog**: note every place the incident proved the catalog wrong or incomplete (a missing dependency, an `unverified` fact now confirmed or refuted, an uncatalogued Resource). Each becomes an action item.
7. **Action items**: one tracked issue per item, filed where the site's procedure says, each linking back to the review and the incident.
8. **Back-fill** the issue links into the document, and update the site's index of reviews if it has one.
9. **Ship**: validate as the site procedure says, then branch, commit, push, pull request. Never commit to the default branch.
10. **Link back**: comment on the tracking issue with the review, the pull request and every action item. Leave the issue open; closing it is a person's decision.
11. **Commit the audit log**: `uv run <plugin root>/scripts/audit.py verify`, put its findings in the review (records, self-reported count, approvals and Tier 2 claims, attempts that never completed, mutations with no authority, capture errors), then `audit.py commit`. Detail: `../start-incident/references/audit-log.md`.

Detail for every step, and the closing checklist: `references/pir-workflow.md`.

## Gotchas

- A review with no action items was either trivial or not investigated deeply enough.
- Action-item priority is not incident severity. A severe incident can produce a low-priority item, and the reverse.
- Do not stop a chain at "the component failed" or "nobody knew". See the stopping rules in `references/infinite-hows.md`.
- Evidence links that expire do not belong in a permanent document. Commit the artefact.
- Step 6 is new to most people and easy to skip. The incident is the best test the catalog ever gets.

## References

- `references/pir-workflow.md` — each step in detail, and the quality checklist
- `references/infinite-hows.md` — the root-cause drilling method
- `../start-incident/references/incident-record.md` — timeline entry types and export
- `../start-incident/references/catalog-in-an-incident.md` — reading the catalog
