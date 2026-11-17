# The analysis file

One file per service: `<analyses_path>/<service>.md` (default `analyses/stpa/`). The file name must be the catalog name of the service it analyses. Schema: `schemas/stpa.schema.json`. Checked by `stpa.py check` and by `validate_site.py`.

## Front matter

```yaml
---
schema_version: 1
service: component:<name>
status: draft            # draft until all four gates passed and every finding is filed; then reviewed
reviewed: 2026-11-17     # date of the last full review; PIRs after it make the analysis due again
scope: >-
  What is inside the boundary and what is not.
losses:
  - {id: L-1, text: ...}
hazards:
  - {id: H-1, text: ..., losses: [L-1]}
controllers:
  - id: C-1
    name: ...
    kind: human | automated | software
    responsibilities: [...]
    process_model: [...]          # optional
    evidence: [...]               # optional
processes:
  - {id: P-1, name: ...}
control_actions:
  - {id: CA-1, from: C-1, to: P-1, action: ...}     # 'to' may be another controller
feedback:
  - {id: F-1, from: P-1, to: C-1, signal: ..., status: present | inadequate | missing, evidence: [...]}
ucas:
  - id: UCA-1
    action: CA-1
    type: not-provided | provided-unsafe | wrong-timing | wrong-duration
    context: ...
    hazards: [H-1]
    evidence: [...]               # or hypothesised: true, never both
scenarios:
  - id: LS-1
    ucas: [UCA-1]
    text: ...
    causal_factors: [flawed-process-model, missing-feedback]
    status: controlled | uncontrolled
    control: ...                  # required when controlled
    finding: https://github.com/<owner>/<repo>/issues/<n>   # required when uncontrolled, once reviewed
    evidence: [...]
proposed:                         # optional: from CAST sections of PIRs, waiting for review
  - {kind: uca | scenario | feedback | controller, text: ..., source: <PIR file name>}
---
```

Evidence entries are plain strings: a PIR file name, `owner/repo#n`, or a path with a line (`roles/x/files/handler.sh:42`). Write enough that a reader can open it.

## Body

Free markdown for people: why the review was run, the boundary, what the diagram shows, the findings in a table, and anything rejected and why. `stpa.py render` writes the diagram between its markers and adds a `## Control structure` section the first time; do not edit inside the markers.

## What `stpa.py check` enforces

Errors:

- The schema, and every id unique and of the right prefix.
- Every reference resolves: hazards → losses, control actions → controllers or processes, feedback, UCAs → control actions and hazards, scenarios → UCAs.
- Every UCA cites evidence or is `hypothesised: true`, not both.
- A controlled scenario names its control. Once `reviewed`, an uncontrolled scenario links its finding.
- The file name matches the service, and the service is in the catalog.

Warnings:

- A control action with no UCA considered; a hazard no UCA leads to.
- An automated controller with no working feedback.
- An uncontrolled scenario with no finding while still a draft.
- Proposed changes waiting; a diagram out of date.
