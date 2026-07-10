---
name: pickup-ticket
description: This skill should be used when the user says "pick up a ticket", "pickup ticket", "work on issue N", "grab homelabia#42", or asks to start work on a GitHub Issue. The ticket reference may arrive as an argument in owner/repo#N form or as a bare issue number.
version: 2.0.0
---

# Pick Up a Ticket

Work a GitHub Issue end-to-end: read it, plan, get approval, implement on a branch, raise a PR, document.

**Hard gate: no implementation work before the user gives express approval of the plan (end of Phase 2). No exceptions — inspection and planning only until then.**

## Resolve the Ticket

- The ticket comes from the skill arguments. If empty, ask: "Which ticket do you want to work on?" and wait for the answer.
- Format: `owner/repo#N` (e.g. `pgmac-net/homelabia#42`). Bare number → ask which repo it belongs to; if the user doesn't know, default to `pgmac-net/homelabia`.
- Use the resolved `owner/repo#N` in every `gh` command below.
- If the repo isn't cloned locally, clone it first: `pgmac-net/*` repos → `~/pgmac/<repo>`, work repos → `~/projects/<repo>`.

## Phase 1 — Pickup

1. Read the ticket and its discussion: `gh issue view <N> --repo <owner>/<repo> --comments`
2. Assign it to yourself: `gh issue edit <N> --repo <owner>/<repo> --add-assignee @me`
3. Mark it in progress: check `gh label list --repo <owner>/<repo>` for an `in-progress` label or existing equivalent and apply it. If none exists, skip — do not invent new labels.
4. Inspect the relevant code until the ticket's scope and touchpoints are understood.
5. Ask the user any clarifying questions.

## Phase 2 — Plan and Approval

1. Write an implementation plan.
2. Post it to the ticket: `gh issue comment <N> --repo <owner>/<repo> --body "<plan>"`
3. Share the ticket URL with the user so they can review the plan.
4. Iterate on feedback — post each revised plan to the ticket as a new comment.
5. Wait for express approval before any implementation.

## Phase 3 — Implement

1. Confirm the current branch, and that the default branch is up to date (`git checkout main && git pull`). Never commit to main/master.
2. Create a branch named `<N>-<short-slug>` (e.g. `42-nrpe-watch-stall-check`).
3. Implement the approved plan.
4. Comment on the ticket at milestones only — work started, significant deviation from the plan, blocked, complete. No play-by-play.

## Phase 4 — Deliver

1. Commit to the branch and push.
2. Raise a PR. Reference the ticket in the PR body as a plain `owner/repo#N` link — not `Closes #N`, since the user closes the ticket themselves after review.
3. Comment on the ticket: summary of the completed work plus the PR link. Do not close the ticket.
4. Update the in-repo markdown documentation to reflect any application usage changes.
5. Create a Notion page describing the work, the process, decisions made, and any deviations from the plan — under the Notion parent page named after the repo, with a markdown copy in the repo's `docs/` directory.
