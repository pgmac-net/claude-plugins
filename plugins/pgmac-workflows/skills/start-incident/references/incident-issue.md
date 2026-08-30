# The Incident Issue

The hold-all record for one incident. Created immediately in Phase 1, before diagnosis — capturing detection time and first symptoms is the entire point; a false alarm just gets closed, which costs nothing.

## The `incident` Label

Every issue created during an incident — the hold-all itself, and any follow-up issue discovered while working it — carries the `incident` label. This is the **one exception** to the "never invent a label that doesn't exist on the target repo" rule that `create-pir` and `pickup-ticket` otherwise follow. Create it if missing, with this exact spec so it matches across repos:

```bash
gh label create incident \
  --repo <owner>/<repo> \
  --color b60205 \
  --description "Work arising from a production incident" \
  2>/dev/null || true
```

The `|| true` absorbs "already exists" — don't treat that as an error.

## Reuse Check

Before creating anything, check for an open incident already in flight:

```bash
gh issue list --repo pgmac-net/incidents --label incident --state open
```

- **Zero results** → create a new issue, no prompt needed.
- **One or more results** → show the title and age of each to the user, ask whether to reuse one or open a new issue. Don't guess.
- **An explicit issue number given as an argument** (e.g. `/start-incident 87 disk full on hal`) → skip the check entirely, use that issue.

## Creating the Issue

```bash
gh label create incident --repo pgmac-net/incidents --color b60205 \
  --description "Work arising from a production incident" 2>/dev/null || true

gh issue create --repo pgmac-net/incidents \
  --title "<short symptom description>" \
  --label incident \
  --body-file <body>
```

Capture the printed URL and `owner/repo#N` — every later comment and every follow-up issue links back to it.

### Body Template

The body holds **stable facts**, edited in place as they firm up — not a log. The log is comments (below).

```markdown
## Incident

**Detected:** <YYYY-MM-DD HH:MM AEST>
**Provisional severity:** <P1-P4, per pir-template.md criteria — expect this to change>
**Phase:** Establishing picture | Diagnosing | Mitigating | Verifying | Resolved
**Affected:** <hosts / services / nodes — update as scope becomes clear>

## Impact

<one or two sentences — what's actually broken for whom, updated as understood>

## Runbook

<matched runbook link, or "no existing runbook covers this — noted as an action item">

---
*This issue is the primary input for the PIR (`/create-pir`) once resolved. Timeline lives in comments below, newest last.*
```

Edit this body in place as fields firm up — provisional severity becomes final, affected scope narrows or widens, phase advances. Don't append a second copy.

## Timeline Comments

Append-only, one comment per milestone, each timestamped AEST:

```markdown
**HH:MM AEST** — <what happened, what was run, what it showed>
```

Post one at each of: first symptom confirmed, monitoring source consulted and what it showed, runbook matched (or gap noted), each diagnostic step with its result, each mutation *and its observed effect*, recovery confirmed. This is deliberately the same granularity `create-pir` Step 1 needs to extract timeline events — matching it here means Step 1 becomes "read the issue," not "reconstruct from memory."

```bash
gh issue comment <N> --repo pgmac-net/incidents --body "**14:32 AEST** — ..."
```

## Follow-Up Issues Found Mid-Incident

Work discovered while diagnosing (a missing check, a repeat offender, delayed cleanup) gets its own issue in whichever repo owns that work — same repo-selection rules `create-pir`'s `github-issues-setup.md` uses. It still needs the `incident` label and a body line linking back:

```markdown
Discovered while working pgmac-net/incidents#<N>.
```
