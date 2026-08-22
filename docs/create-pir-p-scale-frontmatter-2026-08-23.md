# create-pir v1.5.0: P1-P4 severity and frontmatter contract

**Ticket:** [pgmac-net/claude-plugins#8](https://github.com/pgmac-net/claude-plugins/issues/8)
**PR:** [pgmac-net/claude-plugins#9](https://github.com/pgmac-net/claude-plugins/pull/9)
**Branch:** `8-create-pir-p-scale-frontmatter`
**Notion:** [create-pir v1.5.0: P1-P4 severity and frontmatter contract](https://app.notion.com/p/3c4524b4a07781419a24e410104f696f)

## What changed

`pgmac-net/incidents#74` rewrote the PIR contract: metadata moved out of prose under the H1 into YAML frontmatter, and severity moved from `Critical`/`High`/`Medium`/`Low` words to a `P1`–`P4` scale. The `create-pir` skill still wrote PIRs against the old contract, so anything it produced would render with no severity badge, no metadata header, and a full-length nav label — and would fail the build once the severity validator landed.

By the time this ticket was picked up, that validator (`main.py` in the incidents repo) was **already live on `main`**, so the failure mode was already present, not pending. The ticket's premise ("will fail once the validator lands") was updated in the plan before implementation.

## Process

Ran through pickup-ticket end-to-end: read the ticket, assigned, inspected both repos (claude-plugins for the skill, incidents for the canonical contract and validator), grilled the open questions, posted the plan to the ticket, got explicit approval, implemented on Sonnet per the plan's STANDARD rating.

## Decisions made during grilling

1. **Duplication vs. restatement** — `pir-workflow.md` restates only the gotchas (which fields are build-required, the `resolution`-not-`status` trap, `title`-is-nav-label-not-heading) rather than copying the full field table and P1–P4 criteria table out of `pir-template.md`. The template stays the single source of truth; the skill just carries the traps that aren't obvious from reading it once.
2. **Verification gate** — added `mise run build-strict` to the top of Step 9 (Verify, Branch, Commit, Push, PR), before `git add`. The skill writes the frontmatter that `main.py` validates, so it should prove that passes locally rather than discover a failure in CI.
3. **Issue priority vocabulary (the interesting one)** — the ticket asked whether `github-issues-setup.md`'s `High`/`Medium`/`Low` GitHub Issue priority should track the new `P1`–`P4` incident severity scale, since the two now read as the same vocabulary meaning different things. First pass said yes, switch to P1–P4. Checking `pgmac-net/homelabia`'s actual labels (`priority:urgent/high/medium/low`, not a P-scale) and then re-reading `pir-template.md`'s Action Items section surfaced that the incidents repo had **already ruled on this explicitly**:

   > Action priority is not incident severity. Priority ranks how urgently a piece of follow-up work should be done and stays on the High / Medium / Low scale... Severity grades how bad the incident was and uses P1–P4. A P1 outage can produce a Low-priority action, and a P3 can produce a High-priority one.

   Switching the skill to P1–P4 would have put it in direct conflict with the canonical template, the existing Action Items table format, and all 23 already-migrated PIRs. Reverted to keeping `High`/`Medium`/`Low`, and instead added the template's own reasoning as a callout in `github-issues-setup.md` plus an explicit map to homelabia's `priority:*` labels, so the distinction isn't rediscovered per-incident.

## Deviations from plan

None — the plan as approved was implemented as written, including the priority-vocabulary reversal (that reversal happened during grilling, before the plan was posted).

## Files changed

| File | Change |
|---|---|
| `plugins/pgmac-workflows/skills/create-pir/references/pir-workflow.md` | Steps 1, 3, 4, 8, 9 and the quality checklist |
| `plugins/pgmac-workflows/skills/create-pir/references/github-issues-setup.md` | Priority-is-not-severity callout + homelabia label map |
| `plugins/pgmac-workflows/skills/create-pir/SKILL.md` | Frontmatter wording; version 1.4.0 → 1.5.0 |
| `plugins/pgmac-workflows/.claude-plugin/plugin.json` | 1.4.0 → 1.5.0 |
| `.claude-plugin/marketplace.json` | 1.4.0 → 1.5.0 (both fields) |

## Follow-ups

None identified beyond the PR's own test-plan item (verify the next real PIR passes `mise run build-strict`).
