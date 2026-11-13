# The incident record

An incident is one tracking issue. Its **body** holds stable facts, edited in place. Its **comments** are the timeline, append-only. The incident id is `<repo-alias>-<issue number>`, for example `incidents-97`.

## Body

Written by `incident.py open`; change it only with `incident.py set`:

| Field | Flag | Notes |
|---|---|---|
| Severity | `--severity P1..P4`, `--final` | Provisional until `--final` |
| Phase | `--phase` | `Establishing picture`, `Diagnosing`, `Mitigating`, `Verifying`, `Resolved` |
| Affected | `--affected` | Hosts and services, updated as scope becomes clear |
| Impact | `--impact` | One or two sentences: what is broken, for whom |
| Runbook | `--runbook` | The match, or that none covers this |

Never append a second copy of a field, and never paste the log into the body.

## Timeline entries

One comment per entry, always through the script:

```bash
uv run <plugin root>/scripts/timeline.py add --type <type> --text "<text>" [--at <ISO time it happened>]
```

Pass `--at` when recording something after the fact; the default is now. Long text can come on stdin.

| Type | Use for | A good one says |
|---|---|---|
| `discovery` | Something learned: a symptom confirmed, what a monitoring source showed, what a diagnostic found | The command or source, and what it showed |
| `action` | A change made to a system | What was changed, who approved it, **and what happened afterwards** |
| `decision` | A choice between options: runbook matched or not, severity changed, shortcut taken | What was chosen, over what, and why |
| `milestone` | A point reached or missed: mitigated, recovered, a deadline passed | What state was reached, confirmed how |
| `update` | A scheduled status update | State now, what changed since the last, what is next |
| `handover` | Passing the incident to someone else | Use `timeline.py handover` |
| `summary` | A whole-incident digest, at the end or on request | What happened, what fixed it, what is still open |

Comments typed by a person on the issue need no marker. They are kept and exported as `note`.

Granularity: one entry per thing a reviewer would want to find. First symptom, each monitoring source consulted, the runbook decision, each diagnostic with its result, each action with its effect, recovery confirmed.

## Status updates

```bash
uv run <plugin root>/scripts/timeline.py due
```

Returns JSON: `due`, and the minutes since the last `update`, `handover` or `summary`. The interval per severity is set by the site (`incident.update_minutes`). Check before every wait and after every phase.

## Handover

```bash
uv run <plugin root>/scripts/timeline.py handover \
  --state "<where things stand>" --tried "<what has been tried, and its effect>" \
  --hypotheses "<what is still suspected>" --next "<the next step>" --holder "<who or what has it now>"
```

All five fields are required. Write them for someone arriving with no context.

## Export

```bash
uv run <plugin root>/scripts/timeline.py export [--out <file>]
```

Markdown, grouped by date in the site's time zone, with a count by type. This is the review's primary source.

## Follow-up work found mid-incident

Work discovered while diagnosing (a missing check, a repeat offender) gets its own issue in whichever repository owns it, per the site's `review` procedures, with a line linking back to the incident. Do not fix it now unless it is the mitigation.
