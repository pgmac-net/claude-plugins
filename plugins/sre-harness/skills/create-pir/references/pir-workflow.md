# The review workflow in detail

Site specifics come from the site's `review` procedures: template, format, naming, locations, validation command, where issues go. Read them before writing anything. What follows is what every site's review needs.

## Step 1 — Extract

**Metadata**
- Date the incident occurred, start time, resolution time, and so the duration.
- Final severity, by the site's criteria. The tracking issue holds a provisional value; confirm it now and record it with `incident.py set --severity <level> --final`.
- Resolution state: resolved, partially resolved, or still being watched.
- Impact in one or two sentences, in the users' terms. A service's `sre.purpose` in the catalog is a good starting point.
- Affected services and the Resources they run on.

**Timeline**
- First notice, key diagnostics and what they found, each action and its effect, each service's recovery, full resolution.
- Use the export's `action` entries for the changes made, `decision` entries for why, `discovery` entries for evidence.
- Show times in the site's time zone, consistently.

**Root-cause chains**
- One to four distinct chains. Each runs: surface symptom → proximate cause → contributing factors → systemic root cause.
- Look for independent failures that compounded each other.

**Action items**
- Remediation still outstanding, detection gaps, missing runbooks or procedures, longer-term improvements.

**Evidence**
- Gather what timeline entries reference. If a chain rests on a claim that evidence could prove (a resource reaching its ceiling, a metric crossing its threshold) and none was captured, follow the site's `evidence` procedures now. Evidence sources have different lifetimes; the procedure says which.
- If no evidence covers the window, say so in the document and raise the gap as an action item.

Where a detail is missing, make a reasonable inference and mark it as one.

## Step 2 — Infinite How's

Method: `infinite-hows.md`. Target four to seven "how" levels per chain. Stop only at something an action item can close.

## Step 3 — Name

Follow the site's file-naming and title conventions. If it has none: file `YYYY-MM-DD-<three-to-six-word-slug>.md`, dated when the incident began; title `<affected system> <root problem> — <technical detail>`.

## Step 4 — Write

Fill every section of the site's template. No placeholder text. Leave action-item links as placeholders until step 7.

**Evidence in the document.** Embed a graph or log extract where it shows something a sentence cannot prove alone: the onset coinciding with a cause, or the return to baseline after the fix. Skip anything flat or unrelated. Two or three well-chosen items beat a wall of panels. Frame each as baseline → onset → recovery, and give it a caption that names the system, the measure, and what to look at, with times. Commit the artefact beside the document; never link to something that expires.

## Step 5 — Runbooks

For each chain:

- **Create** when recovery is repeatable with concrete diagnostics, the cause is within the site's control, and no runbook covers it.
- **Extend** when a runbook covers the same service or symptom and gains from a new failure mode.
- **Skip** when the failure is a true one-off, recovery needed judgment that will not reduce to a procedure, or a runbook already covers it. State the reason.

A runbook needs: the exact diagnostic commands that identified this failure, the exact recovery steps that fixed it (diagnosis and recovery in separate sections), verification that confirms resolution, and a link to this review. Use the site's runbook template.

When a runbook is created or extended, add it to the affected service's `sre.runbooks` in the catalog, as part of step 6.

## Step 6 — Catalog findings

Go through the timeline's `discovery` entries and the chains, and list each place the catalog was wrong or thin:

- A dependency that exists and is not listed, or is listed and is false.
- An `unverified` fact the incident confirmed (remove it from `unverified`) or refuted (correct it).
- A Resource that mattered and belongs to no service.
- A service whose tier no longer matches what its outage cost.
- A missing check: the catalog lists monitoring that did not fire, or lists none.

Each is an action item against the site repository. Small, certain corrections can go in a pull request to the site repository now; anything needing the owner's judgment becomes an issue.

## Step 7 — Action items

One issue per item, in the repository that owns the work, per the site's procedure. Each states which chain it addresses and what gap it closes, and links to the review and to the incident. Apply only labels the target repository already has, except where the site's procedure says a label may be created. Record each issue's reference.

## Step 8 — Back-fill and index

Replace every placeholder in the document with the real issue link. If the site keeps an index of reviews, add this one in the place and format its procedure gives.

## Step 9 — Ship

Run the site's validation command and fix what it reports before committing. Then, from an up-to-date default branch: a new branch, one commit with the review, any runbooks, any evidence files and the index; push; open a pull request summarising the incident, its root causes, the action items filed, and any runbooks created or changed.

## Step 10 — Link back

Comment on the tracking issue: the review, the pull request, every action item. Do not close it.

## Step 11 — Commit the audit log

```bash
uv run <plugin root>/scripts/audit.py verify
```

State its findings in the review, plainly: how many records, how many were self-reported rather than captured by a hook, any attempt that never completed (a denied or interrupted command, worth a sentence each), and any capture errors. If the chain is broken, say so and stop: that is a finding for the person, not something to repair.

Then, with the review's pull request open:

```bash
uv run <plugin root>/scripts/audit.py commit
```

This opens a second pull request, against the site's private audit location, and stops capture. Link it from the tracking issue only if that issue is private; the audit log's contents never go in a public place.

## Quality checklist

**Review**
- [ ] The site's validation passes.
- [ ] Every template section is filled; no placeholders remain.
- [ ] Times are in the site's time zone throughout.
- [ ] Each chain drills to something actionable, not just a proximate cause.
- [ ] The summary says what happened, why, and what fixed it.
- [ ] What went well and what did not are both recorded.
- [ ] Every action item is a real, linked issue.
- [ ] Evidence that proves cause or recovery is embedded and committed, or its absence is an action item.
- [ ] Final severity is recorded on the tracking issue.

**Runbooks**
- [ ] The create, extend or skip decision is explicit for each chain.
- [ ] Commands are the ones from this incident, not generic examples.
- [ ] Diagnosis and recovery are separate; verification is present.

**Catalog**
- [ ] Each place the incident contradicted or outran the catalog is an action item or a correction.

**Incident**
- [ ] The timeline export was the primary source.
- [ ] Every action item links back to the incident.
- [ ] The link-back comment is posted, and the issue was not closed.
- [ ] The audit log is verified, its findings are in the review, and it is committed.
