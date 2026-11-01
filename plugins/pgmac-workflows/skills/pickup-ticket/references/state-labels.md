# State Labels

`pickup-ticket` shows where a ticket is with one state label at a time. These three labels are the **only** labels the skill may create. Any other label must already exist on the repo — never invent one.

| State | Colour | Description | Applied when |
|---|---|---|---|
| `in-progress` | `61deb3` | Being worked on | Phase 1 pickup; again when unblocked |
| `blocked` | `d93f0b` | Blocked, waiting on something external | Phase 4, when work is blocked |
| `in-review` | `0075ca` | PR raised, awaiting review | Phase 5, after the PR is raised |

The skill never applies a done label and never closes the ticket. The label stays `in-review` until the user closes it.

## Resolve the label name

Run `gh label list --repo <owner>/<repo> --limit 200`, then for each state, in order:

1. **Exact match** — a label named `<state>` or `status:<state>`. Use it.
2. **Equivalent** — a label that clearly means the same state, e.g. `wip`, `doing`, `status:in progress`, `needs-review`, `status:blocked`. Use it.
3. **Create** — none found:
   - the repo has any `status:*` label → create `status:<state>`
   - otherwise → create plain `<state>`

Create with the colour and description from the table. Never pass `--force`: it recolours a label that already exists.

```bash
gh label create "<name>" --repo <owner>/<repo> \
  --color <colour> --description "<description>"
```

If the command reports the label already exists, treat it as a match and re-read `gh label list`. If it fails for any other reason (usually no triage permission on the repo), warn in the session and carry on without the label — a missing label never blocks ticket work. Existing labels are never recoloured or renamed.

## Swap states

Only one of the three states is on the ticket at a time. Add the new label and remove the previous one in a single call, using the resolved names:

```bash
gh issue edit <N> --repo <owner>/<repo> \
  --add-label "<new>" --remove-label "<previous>"
```

Leave every other label alone: `status:todo`, `status:backlog`, priority labels and the rest are not state labels for this skill, so a ticket can legitimately show `status:todo` next to `in-progress`.

`--remove-label` errors (`'<name>' not found`) when the repo has no such label, so only pass it for a state label actually on the ticket — check with `gh issue view <N> --repo <owner>/<repo> --json labels --jq '.labels[].name'`. With no previous state label, use `--add-label` alone.
