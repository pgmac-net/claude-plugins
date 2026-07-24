---
name: pickup-ticket
description: This skill should be used when the user says "pick up a ticket", "pickup ticket", "work on issue N", "grab homelabia#42", or asks to start work on a GitHub Issue. The ticket reference may arrive as an argument in owner/repo#N form or as a bare issue number.
version: 2.2.0
---

# Pick Up a Ticket

Work a GitHub Issue end-to-end: read it, grill it, plan, get approval, implement on a branch, raise a PR, document.

**Hard gate: no implementation work before the user gives express approval of the plan (end of Phase 3). No exceptions — inspection, grilling, and planning only until then.**

## Resolve the Ticket

- The ticket comes from the skill arguments. If empty, ask: "Which ticket do you want to work on?" and wait for the answer.
- Format: `owner/repo#N` (e.g. `pgmac-net/homelabia#42`). Bare number → ask which repo it belongs to; if the user doesn't know, default to `pgmac-net/homelabia`.
- Use the resolved `owner/repo#N` in every `gh` command below.
- If the repo isn't cloned locally, clone it first: `pgmac-net/*` repos → `~/pgmac/<repo>`, work repos → `~/projects/<repo>`.

## Model Selection

Planning happens on the most capable model; implementation happens on a cheaper model matched to the ticket's complexity. Claude cannot switch the session's model itself — check the running model at each boundary and ask the user to switch with `/model`. If the user declines a switch, note it and continue on the current model — never block on a model change.

**Planning model (Phases 1–3):** Claude Fable 5 — `/model claude-fable-5`. Fallback: the latest Claude Opus (`/model opus`) if Fable 5 is unavailable (model-not-found error, or absent from the `/model` picker after retirement).

At skill start, check which model the session is running (stated in the system prompt, or via `/status`). If it isn't the planning model, ask: "Planning phase — switch with `/model claude-fable-5` (or `/model opus` if Fable 5 is gone), then say continue." Wait for the switch or a decline before proceeding.

**Complexity tiers** — assigned during Phase 3 as part of the plan. When in doubt between two tiers, pick the higher one.

| Tier | Criteria | Implementation model |
|---|---|---|
| TRIVIAL | Mechanical change, docs, config tweak, single-file fix, no design decisions | Haiku — `/model haiku` |
| STANDARD | Typical feature or bugfix, a few files, tests | Sonnet — `/model sonnet` |
| COMPLEX | Cross-cutting refactor, unfamiliar subsystem, infra-critical or concurrency work, migration | Fable 5 — `/model claude-fable-5` (fallback Opus — `/model opus` — when Fable 5 is unavailable) |

## Phase 1 — Pickup

1. Read the ticket and its discussion: `gh issue view <N> --repo <owner>/<repo> --comments`
2. Assign it to yourself: `gh issue edit <N> --repo <owner>/<repo> --add-assignee @me`
3. Mark it in progress: check `gh label list --repo <owner>/<repo>` for an `in-progress` label or existing equivalent and apply it. If none exists, skip — do not invent new labels.
4. Inspect the relevant code until the ticket's scope and touchpoints are understood.

## Phase 2 — Grill the Ticket

Once the ticket and its code touchpoints are understood, run a grilling session on the ticket's requirements before writing any plan — same pattern as `grill-with-docs`: interview relentlessly using the `grilling` skill, capturing resolved terminology and hard-to-reverse decisions with the `domain-modeling` skill as they crystallise.

1. Interview one question at a time, each with a recommended answer, walking down the ticket's requirements until scope, edge cases, and any ambiguous terms are pinned down.
2. Look facts up in the codebase instead of asking when the answer is discoverable there.
3. Let `domain-modeling` update `CONTEXT.md` and, sparingly, write an ADR in the target repo as terms/decisions resolve — lazily, only when there's something real to write.
4. Do not proceed to Phase 3 until a shared understanding is reached.

## Phase 3 — Plan and Approval

1. Write an implementation plan. End it with a complexity line naming the tier and implementation model from the Model Selection table, e.g. `Complexity: STANDARD — implement on Sonnet`. Approving the plan also approves the model choice.
2. Post it to the ticket: `gh issue comment <N> --repo <owner>/<repo> --body "<plan>"`
3. Share the ticket URL with the user so they can review the plan.
4. Iterate on feedback — post each revised plan to the ticket as a new comment.
5. Wait for express approval before any implementation.

## Phase 4 — Implement

0. Switch to the implementation model: if the session isn't already on the tier model recorded in the approved plan, ask the user to switch (e.g. "Plan rated STANDARD — switch with `/model sonnet`, then say continue") and wait. If the user declines or implementation ends up on a different model than the plan recorded, add a one-line note to the work-started ticket comment.
1. Confirm the current branch, and that the default branch is up to date (`git checkout main && git pull`). Never commit to main/master.
2. Create a branch named `<N>-<short-slug>` (e.g. `42-nrpe-watch-stall-check`).
3. Implement the approved plan.
4. Comment on the ticket at milestones only — work started, significant deviation from the plan, blocked, complete. No play-by-play.

## Phase 5 — Deliver

1. Commit to the branch and push.
2. Raise a PR. Reference the ticket in the PR body as a plain `owner/repo#N` link — not `Closes #N`, since the user closes the ticket themselves after review.
3. Comment on the ticket: summary of the completed work plus the PR link. Do not close the ticket.
4. Update the in-repo markdown documentation to reflect any application usage changes.
5. Create a Notion page describing the work, the process, decisions made, and any deviations from the plan — under the Notion parent page named after the repo, with a markdown copy in the repo's `docs/` directory.
