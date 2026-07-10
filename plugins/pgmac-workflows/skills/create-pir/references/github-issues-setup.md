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

**Recommended description format:**
```markdown
## Context

This issue was created from PIR: [PIR Title](https://github.com/pgmac-net/incidents/blob/main/src/incidents/<filename>.md)

## Why This Is Needed

[Which root cause chain this addresses and what gap it closes]

## Priority

<High | Medium | Low> (from PIR — only include this section if no matching priority/severity label exists on the repo)

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
