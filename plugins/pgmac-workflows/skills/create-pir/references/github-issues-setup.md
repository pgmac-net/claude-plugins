# GitHub Issues Configuration for Homelab Incidents

Linear is decommissioned for this homelab. All PIR action items become GitHub Issues.

## Repo Selection

Pick the repo whose code/config the action item actually touches:

| Action item touches | Repo |
|---|---|
| An ansible role or playbook | that role/playbook's repo |
| A Terraform module or stack | that module/stack's repo |
| An app's own code or Helm chart | that app's repo |
| Docker-Nagios changes | `pgmac-net/Docker-Nagios` (always the fork, never upstream `JasonRivers/Docker-Nagios`) |
| Cluster-operational item with no single owning repo (dqlite, kubelite, calico, generic k8s ops) | `pgmac-net/homelabia` (fallback) |

If genuinely unclear which repo owns the work, default to `pgmac-net/homelabia`.

## Creating Issues via gh CLI

```bash
gh issue create --repo <owner>/<repo> \
  --title "<action item text>" \
  --body "<description>" \
  --label "<matching existing label, if any>"
```

Check labels before applying one — do not invent labels that don't exist on the target repo:
```bash
gh label list --repo <owner>/<repo>
```

**The one exception: `incident`.** Every action item created from a PIR that traces back to a live incident (i.e. an incident tracking issue exists — see `start-incident`) gets the `incident` label, creating it in the target repo first if missing, with this exact spec so it matches across repos:
```bash
gh label create incident --repo <owner>/<repo> \
  --color b60205 \
  --description "Work arising from a production incident" \
  2>/dev/null || true
```
Every other label still must pre-exist — this exception is scoped to `incident` alone, not a general license to invent labels.

**Recommended description format:**
```markdown
## Context

This issue was created from PIR: [PIR Title](https://github.com/pgmac-net/incidents/blob/main/src/incidents/<filename>.md)

## Why This Is Needed

[Which root cause chain this addresses and what gap it closes]

## Priority

<High | Medium | Low> (from PIR — only include this section if no matching priority/severity label exists on the repo)

**Action priority is not incident severity.** The PIR's `severity` frontmatter grades how bad the incident was, on the `P1`–`P4` scale. This grades how urgently the follow-up work should be done, and stays on `High`/`Medium`/`Low` because it maps to the repo's priority labels. A `P1` incident can produce a `Low`-priority action item, and a `P3` can produce a `High`-priority one.

On `pgmac-net/homelabia`, map to the existing labels:

| Priority | Label |
|---|---|
| High | `priority:high` |
| Medium | `priority:medium` |
| Low | `priority:low` |

`priority:urgent` sits above this scale — it's for issues more urgent than any PIR action item, so PIR-derived items don't use it.

## Acceptance Criteria

- [ ] [Specific measurable outcome 1]
- [ ] [Specific measurable outcome 2]
```

## Linking Back to PIR

`gh issue create` prints the issue URL directly on success — capture it, don't reconstruct it manually. Format is:
`https://github.com/<owner>/<repo>/issues/<N>`

In the PIR Action Items table:
```markdown
| 1 | Add NRPE check for kubelet watch stream staleness | High | [pgmac-net/homelabia#42](https://github.com/pgmac-net/homelabia/issues/42) |
```

In Preventive Measures section:
```markdown
- Action: Add NRPE check
- Issue: [pgmac-net/homelabia#42](https://github.com/pgmac-net/homelabia/issues/42)
```

## Linking Back to the Incident Tracking Issue

When an action item traces back to a live incident, also link the other direction — from the new action-item issue back to the incident tracking issue in `pgmac-net/incidents` — by adding a line to the description's `## Context` section:
```markdown
Discovered while working pgmac-net/incidents#<N>.
```
This is separate from the PIR-workflow's own Step 10, which comments on the incident issue listing every action item created — the two links together make the relationship navigable in both directions.

## Issue Title Conventions

Use action-oriented titles that describe what to build or fix, not what went wrong:

**Good titles:**
- `Add NRPE check: detect kubelet watch stream stall (scheduled pods not in /proxy/pods for >120s)`
- `Add VXLAN VTEP route correctness monitoring on all peer nodes`
- `Document canonical watch stall recovery: cordon → k8s-dqlite restart → kubelite restart`
- `Fix kine/dqlite watch reliability: add retry-with-backoff on database is locked`

**Avoid:**
- `Fix the watch stall issue` (too vague)
- `k8s03 kubelet broken` (describes incident not action)
- `Research kine` (not actionable)

## Historical Linear Tickets

Old `PGM-XXX` references in prior PIRs/runbooks are historical and still valid as past-incident pointers (`https://linear.app/pgmac-net-au/issue/PGM-NNN`). Don't rewrite them. When a new action item duplicates or extends work tracked by an old `PGM-XXX` ticket, note the historical reference in the new GitHub Issue's description rather than trying to link back into Linear.
