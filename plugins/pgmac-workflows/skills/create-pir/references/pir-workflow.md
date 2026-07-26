# Full PIR Workflow

The step-by-step procedure. Read `pir-template.md` and `runbook-template.md` (paths in SKILL.md) before writing either document — they carry the section-by-section guidance.

## Step 1 — Extract Incident Details from Conversation

Read the full conversation history to collect:

**Metadata to find:**
- Incident date (when it occurred, not today unless it happened today)
- Start time and resolution time → calculate duration
- Severity: Critical / High / Medium / Low (guide is in the PIR template)
- Status: Resolved / Partially Resolved / Monitoring
- Affected services, pods, and Kubernetes nodes

**Timeline events (AEST = UTC+10):**
- When the incident was first noticed
- Key diagnostic commands run and what they found
- Each fix applied and its effect
- When each affected service recovered
- Full resolution time

**Root cause chains:**
- Identify 1–4 distinct failure chains
- Each chain: surface symptom → proximate cause → contributing factors → systemic root cause
- Look for multiple independent failures that compounded each other

**Action items (what the incident revealed needs fixing):**
- Immediate remediations still outstanding
- Monitoring/alerting gaps
- Missing runbooks or documented procedures
- Longer-term architectural improvements

If details are ambiguous or missing from the conversation, make a reasonable inference and note it clearly in the document.

## Step 2 — Apply Infinite How's Root Cause Analysis

For each distinct failure chain, drill from symptom to systemic root cause. Full methodology in `infinite-hows.md`.

**Format each chain as nested H4/H5 questions:**

```markdown
#### Chain N: [Symptom] — [Short Title]

##### How did [symptom] happen?
[Proximate cause]

##### How did [proximate cause] happen?
[Contributing cause]

##### How did [contributing cause] happen?
[Deeper cause]

... continue until reaching a systemic/actionable root cause

##### How was [root cause] not prevented or detected?
[The process/monitoring/knowledge gap]
```

**Stop drilling when reaching:**
- A missing monitoring or alerting capability (no alert existed)
- A missing runbook or undocumented procedure
- An upstream software bug not actionable on our side
- A deliberate architectural trade-off
- An external dependency or hardware failure

Target 4–7 "how" levels per chain.

## Step 3 — Choose Filename and Title

Filename: `YYYY-MM-DD-<slug>.md`
- Date: when the incident first occurred
- Slug: lowercase, hyphens, 3–6 words summarising the incident
- Example: `2026-05-18-k8s03-kine-watch-vxlan-corruption.md`

Title: `[Affected System] [Root Problem] — [Technical Detail]`
- Example: `k8s03 Extended Recovery — kine Watch Corruption and VXLAN Route Blackhole`

## Step 4 — Write the PIR Document

Read the template and section guidance at `/home/paul/pgmac/incidents/src/doc-templates/pir-template.md` before writing. Write the PIR to:
`/home/paul/pgmac/incidents/src/incidents/<filename>.md`

**MkDocs frontmatter tags** — include all relevant:
- Nodes: `k8s01`, `k8s02`, `k8s03`
- Technologies: `calico`, `kine`, `dqlite`, `kubelet`, `containerd`, `argocd`, `openebs`
- Failure types: `watch-stream`, `vxlan`, `ipam`, `pleg`, `crash-loop`, `oom`
- Domain: `networking`, `storage`, `scheduling`

Leave Action Items table issue links as `ISSUE-XXX` placeholders — fill in after creating issues in Step 6.

## Step 5 — Evaluate and Create Runbooks

Read the runbook template at `/home/paul/pgmac/incidents/src/doc-templates/runbook-template.md` before making any decisions or writing any files.

### Should a runbook be created or updated?

For each distinct root cause chain in the PIR, weigh:

- **Create new** when the recovery procedure is repeatable with concrete diagnostic commands, the root cause is internal infrastructure (not a one-off external event or unfixable upstream bug), and no existing runbook covers it.
- **Extend existing** when a runbook already covers the same service/symptom area and would benefit from a new failure-mode section.
- **Skip** when the failure is a true one-off, recovery required a judgment call that can't be reduced to a procedure, or an existing runbook already covers it adequately.

### Check existing runbooks

List the files in `src/runbooks/` and read any that might overlap with the incident's failure modes.

### Choose runbook pattern

From the runbook template, choose:
- **Simple pattern** — one failure mode, one root cause, one recovery path
- **Multi-mode pattern** — same symptom, multiple distinct root causes requiring different recovery paths

Use multi-mode when this incident's failure mode could logically be grouped with an existing simple runbook's failure (consider updating that runbook to multi-mode). Otherwise use simple.

### Runbook filename convention

`<service>-<failure-description>.md` — lowercase, hyphens, concise. Examples: `kubelet-silent-stall.md`, `dqlite-write-contention.md`, `calico-cni-unauthorized.md`

### Write the runbook

Write new runbooks to `src/runbooks/<filename>.md`. Update existing runbooks in place.

**Required for every runbook:**
- Frontmatter tags: always include `runbook`, plus technology and platform tags
- Reference the PIR that documented this failure: `**PIR:** [Title](../incidents/YYYY-MM-DD-filename.md)`
- Exact diagnostic commands from the incident (the ones that actually identified the failure)
- Exact recovery steps from the incident (the ones that actually fixed it)
- Verification commands that confirm resolution
- Cross-link to related runbooks where relevant

**For multi-mode pattern additions:** update the Quick Reference table to include the new failure mode column.

## Step 6 — Create GitHub Issues for All Action Items

Read `github-issues-setup.md` first — it has the repo-selection table, issue description format, and title conventions.

For each action item in the PIR (including any "create runbook" action items, which are now satisfied):

1. Pick the target repo per the reference's selection table; default to `pgmac-net/homelabia` when no single repo owns the work.
2. Check existing labels (`gh label list --repo <owner>/<repo>`) and apply a matching priority/severity label if one exists — do not invent new labels.
3. Create the issue with `gh issue create`, using the description format from the reference (root cause chain addressed, link to PIR, runbook link if one was created, priority in body only when no matching label exists).
4. Record each new issue's `owner/repo#N` reference and URL (the `gh issue create` output prints the URL directly).

## Step 7 — Back-Fill GitHub Issue Links into PIR

Replace each `ISSUE-XXX` placeholder in the Action Items table with the real link:
```
[owner/repo#N](https://github.com/owner/repo/issues/N)
```

The Action Items table's link column header is `GitHub` (`| # | Action | Priority | GitHub |`) — keep this consistent across PIRs.

Also add links in the Preventive Measures section where each action references a GitHub Issue.

## Step 8 — Update the Incidents Index

Edit `/home/paul/pgmac/incidents/src/incidents/index.md`. Insert a new row at the **top** of the table (newest-first):
```
| YYYY-MM-DD | [Full PIR Title](filename.md) | Severity | ~Xh Ym |
```

Duration format: `~2h10m`, `~45m`, `~7h`, `~3 days` — match the style of existing rows.

## Step 9 — Branch, Commit, Push, PR

```bash
cd /home/paul/pgmac/incidents

# Ensure main is up to date
git checkout main && git pull

# Create branch
git checkout -b docs/pir-<slug>

# Stage PIR, index, and any runbook files created/modified
git add src/incidents/<filename>.md src/incidents/index.md
# If runbooks were created or modified:
git add src/runbooks/<runbook-filename>.md

# Commit (append the standard Claude Code co-author trailer per the session's commit rules)
git commit -m "docs(pir): <incident title> (<primary-issue-ref>)

<one-line summary of what the PIR documents>
<if runbooks created: 'Adds runbook for <failure mode>'>"

# Push and raise PR
git push -u origin docs/pir-<slug>

gh pr create \
  --title "docs(pir): <title> (<primary-issue-ref>)" \
  --body "$(cat <<'EOF'
## Summary

- <bullet: what incident this documents>
- <bullet: key root causes — one per chain>
- <bullet: action items created as GitHub Issues>
- <bullet: runbooks created or updated, if any>

## Test plan

- [ ] Validate workflow passes (MkDocs strict build)
- [ ] New PIR renders correctly in site
- [ ] Index link resolves to the new document
- [ ] New/updated runbooks render correctly (if applicable)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Quality Checklist

Before committing, verify every item:

**PIR:**
- [ ] All template sections filled — no placeholder text or TODO markers
- [ ] Timeline entries use AEST times consistently throughout
- [ ] Each Infinite How's chain drills to a systemic/actionable root cause (not just proximate cause)
- [ ] Infinite How's chains labelled Chain 1, Chain 2, etc. with descriptive subtitles
- [ ] Action Items table has real GitHub Issue links (not placeholders)
- [ ] Frontmatter tags cover all affected nodes and technologies
- [ ] Index row: correct date, title matches H1, links to correct filename, duration matches metadata
- [ ] Severity in index row matches severity in document metadata
- [ ] Executive Summary mentions all three key elements: what happened, why, what fixed it
- [ ] Lessons Learned has both "What Went Well" and "What Didn't Go Well" sections

**Runbooks (if created or updated):**
- [ ] Runbook decision was made explicitly — either a runbook was created, an existing one was updated, or skipped with a clear reason
- [ ] Correct pattern chosen (simple vs multi-mode)
- [ ] Frontmatter includes `runbook` tag plus technology/platform tags
- [ ] PIR cross-reference link present (`**PIR:**` metadata line)
- [ ] Diagnostic commands are the exact commands from the incident (not generic examples)
- [ ] Recovery steps are numbered, with code blocks and expected output where helpful
- [ ] Verification section confirms resolution with concrete checks
- [ ] If multi-mode: Quick Reference table is updated to include the new failure mode
- [ ] If extending an existing runbook: the new failure mode section follows the same style as existing modes
