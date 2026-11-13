# sre-harness incident skills (M2, part 1 of 4)

Ticket: pgmac-net/homelabia#236. Parent plan: pgmac-net/homelabia#223.

## What was built

`sre-harness` v0.3.0 adds portable versions of `start-incident` and `create-pir`.
They read the configured site and its catalog and hold no site knowledge of
their own.

- **`scripts/incident.py`** — opens, updates and resolves the tracking issue;
  refuses to open a duplicate; `--drill` tracks a practice incident in a
  private repository.
- **`scripts/timeline.py`** — the only writer of timeline entries. Seven types,
  one comment each, with a hidden marker line. Also `due` (is a status update
  owed for this severity?), `handover` and `export`.
- **Skills `start-incident` and `create-pir`**, with references for the triage
  stance, the incident record, using the catalog in an incident, the review
  workflow and Infinite How's.
- **`site.yaml` schema version 3**: an `incident` section with time zone,
  severity criteria, update intervals, site procedures per stage, evidence
  directory, drill tracker, and optional settings later parts will use.
- **Site procedures** (ADR 0010): the skills say when, the site repository
  says how.

The old skills in `pgmac-workflows` are untouched and stay the default until
the new ones have been through a drill and a real incident.

## Parity with the old skills

### start-incident

| Old behaviour | Now |
|---|---|
| Role and authority: diagnostics free, mutations confirmed with blast radius | `start-incident` SKILL "Authority"; `references/triage-stance.md` |
| Hardcoded repositories and paths | `site.yaml`: `repos`, `tracker`, `incident` |
| Phase 1: open the record first; check for an open incident | `incident.py open`; exit 3 lists open incidents |
| `incident` label, created if missing, fixed colour | `incident.py` creates it with the same colour and description |
| Issue body template, edited in place | `incident.py open` and `set`; same fields. "Provisional severity" is now "Severity … (provisional)" |
| Timeline comments, one per milestone | `timeline.py add`; entries now carry a type and a machine marker |
| Phase 2: three monitoring tiers, known gotchas | Site `triage` procedures (moved, not rewritten) |
| Provisional severity from the PIR template's criteria | `incident.severity_criteria` points at that document |
| Copy SAR files early; graph with `sargraph` | Site `evidence` procedure |
| Graph the Nagios checks that alerted; `nagios-graph.sh` | Site `evidence` procedure and `procedures/scripts/` |
| Phase 3: runbook match; pull the checkout first | SKILL phase 3; starts from the runbooks on the affected services' catalog entries |
| Phase 4: log each action and its effect | `action` entries |
| Phase 5: verify through the source that flagged it; regraph | SKILL phase 5; site `evidence` procedure |
| Phase 6: hand off to the review; issue stays open | SKILL phase 6; `incident.py close` never closes the issue |
| Stabilise first; when to stop and ask | `references/triage-stance.md` |
| Comms: read the status channel, never post | Site `comms` procedure |
| Follow-up issues found mid-incident | `references/incident-record.md`; site `review` procedure for where they go |

New: catalog lookups (services affected, tier, dependants, runbooks), status
updates on a per-severity interval, handover, summary, drills.

### create-pir

| Old behaviour | Now |
|---|---|
| Step 1: extract, tracking issue as primary source | `timeline.py export` is the primary source |
| Step 2: Infinite How's | `references/infinite-hows.md`, examples made generic |
| Steps 3–4: naming, frontmatter rules, tags, template | Site `review` procedure; generic defaults in `references/pir-workflow.md` |
| Graph selection, framing and captions | Principles in `pir-workflow.md`; commands in the site `evidence` procedure |
| Step 5: runbook create, extend or skip | `pir-workflow.md` step 5 |
| Step 6: one issue per action item; repository selection; `incident` label | `pir-workflow.md` step 7; site `review` procedure |
| Step 7–8: back-fill links; update the index | `pir-workflow.md` step 8; index format in the site procedure |
| Step 9: strict build, branch, commit, PR | `pir-workflow.md` step 9; validation command in the site procedure |
| Step 10: link back; do not close | `pir-workflow.md` step 10 |
| Quality checklist | `pir-workflow.md`, generic items; site items in the site procedure |

New: step 6, catalog findings. The incident's contradictions of the catalog
become corrections or action items. Final severity is recorded on the tracking
issue.

Nothing was dropped. Everything site-specific moves to the site repository in
a follow-up change there; until that lands, the new skills have no site
procedures to read.

## Decisions

| Topic | Decision |
|---|---|
| Timeline entry | One comment: hidden marker (type, UTC time) plus a visible line. Seven types |
| Incident id | `<repo-alias>-<issue number>`; no second numbering scheme |
| Status updates | Interval per severity from the site; the agent asks `timeline.py due`. No timer exists in a skill |
| Site know-how | Site procedures, loaded per stage |
| Drills | Tracked in a private repository; never mutate |
| Shared skill names | Old and new share names. Claude Code separates them by plugin prefix; Pi and OpenCode keep the first found, the old one |

## Verification

- 107 tests pass; ruff clean; pylint 10.00/10. `gh` is replaced by a recorder in tests.
- Live smoke test against the code host: a drill issue opened in a private
  repository, typed entries posted, body edited, `due` and `export` run,
  incident resolved. The test issue was then closed.
- Identifier scan clean, generic and against a private denylist.
- Not verified: the two skills run end to end by an agent. That is the drill,
  after part 3.

## Still to come in M2

Audit log (part 2), tiered autonomy (part 3), advisory helper (part 4).
