---
name: create-pir
description: This skill should be used when the user says "create a PIR", "write a post-incident review", "generate an incident report", "document this incident", "write a post-mortem", "capture this incident", or asks to create formal incident documentation from the current session. Trigger whenever an incident has been resolved and the user wants structured documentation.
version: 1.6.0
---

# Create Post-Incident Review (PIR)

Create a formal PIR from the current Claude session: apply Infinite How's root cause analysis, evaluate/write runbooks for repeatable failure modes, create GitHub Issues for action items, commit, and raise a PR — end-to-end in one flow, not just described.

If the incident was worked with `start-incident`, an incident tracking issue in `pgmac-net/incidents` already holds the timeline and metadata — read it first (`gh issue view --comments`) and treat it as the primary source, with the conversation as corroboration. This skill still works with no incident issue at all; extraction falls back to the conversation alone.

## Repo and Paths

| Resource | Path |
|----------|------|
| Incidents repo | `/home/paul/pgmac/incidents/` |
| PIR documents | `src/incidents/` |
| Runbooks | `src/runbooks/` |
| Index page | `src/incidents/index.md` |
| PIR template | `src/doc-templates/pir-template.md` |
| Runbook template | `src/doc-templates/runbook-template.md` |
| Document format | MkDocs markdown with YAML frontmatter tags |

## The Ten Steps

1. **Extract** incident metadata, timeline, root-cause chains, and action items — from the incident tracking issue if one exists (primary source), the conversation otherwise.
2. **Root-cause** each chain with Infinite How's (methodology: `references/infinite-hows.md`).
3. **Name** the document — filename and title conventions.
4. **Write** the PIR against the template, with MkDocs frontmatter — `title`, `date`, `severity` (`P1`–`P4`), `duration`, `resolution`, `impact`, `tags`.
5. **Runbooks** — decide create / extend / skip per failure chain, using the runbook template.
6. **Issues** — one GitHub Issue per action item, labelled `incident` (repo selection and format: `references/github-issues-setup.md`).
7. **Back-fill** the `ISSUE-XXX` placeholders in the PIR with real issue links.
8. **Index** — add the new PIR row, newest-first.
9. **Ship** — branch, commit, push, PR.
10. **Link back** — if an incident issue exists, comment on it with the PIR, PR, and action-item links. Leave it open; closing is a human decision.

Full detail, exact commands, and the closing quality checklist for every step: `references/pir-workflow.md`.

## Additional Resources

- `references/pir-workflow.md` — the full step-by-step procedure and quality checklist
- `references/infinite-hows.md` — root cause drilling methodology
- `references/github-issues-setup.md` — GitHub Issues field reference and repo-selection rules
- `/home/paul/pgmac/incidents/src/doc-templates/pir-template.md` — PIR template with section guidance (also published at the incidents site under Templates → PIR Structure Template)
- `/home/paul/pgmac/incidents/src/doc-templates/runbook-template.md` — runbook template, simple and multi-mode patterns
- Default fallback repo for action items with no clear owning repo: `pgmac-net/homelabia`
