# Infinite How's Methodology

## Overview

The Infinite How's is a root cause analysis technique that prevents stopping at proximate causes. It is a direct antidote to the "5 Whys" trap where the analysis stops too early at a convenient explanation.

The key difference from "5 Whys":
- **5 Whys** asks "why?" and tends to assign blame or stop at the first systemic cause
- **Infinite How's** asks "how did this happen?" and keeps drilling through technical mechanisms until reaching an actionable, preventable gap

## The Core Rule

**Never accept a surface answer. For every causal statement, ask: "But HOW, specifically, did that happen?"**

A surface answer is one that:
- Could have been stated before looking at any evidence
- Doesn't name a specific component, mechanism, or configuration
- Stops at a human or process element without explaining the technical path
- Is satisfied by "because X failed" without explaining how X failed

## Chain Structure

Each chain starts with a single observable symptom (the thing that triggered the investigation) and ends at a systemic root cause (the gap that, if closed, would prevent this class of failure).

```
Chain N: [Observable Symptom] — [Short Title]

How did [symptom] occur?
→ [Proximate technical cause: the immediate mechanism]

How did [proximate cause] occur?
→ [Enabling condition: what made the proximate cause possible]

How did [enabling condition] exist?
→ [Deeper contributing factor]

How did [contributing factor] persist undetected?
→ [Monitoring / knowledge / process gap]

How was [gap] not closed before this incident?
→ [Systemic root cause: the process, culture, or architecture decision that allowed the gap]
```

## When to Stop Drilling

Stop at the level where an action item can be defined. Stopping points:

1. **Missing detection** — "There was no alert / no NRPE check / no monitoring for X" → action: add monitoring
2. **Missing procedure** — "There was no runbook for X" → action: document recovery procedure
3. **Missing safeguard** — "There was no retry / circuit breaker / health check for X" → action: add the safeguard
4. **Known upstream bug** — "This is a bug in software version X, fixed in Y" → action: upgrade
5. **Deliberate trade-off** — "This is a known limitation of the architecture accepted for reason Z" → document and accept or change the trade-off
6. **External failure** — hardware fault, ISP outage, third-party API down → action: improve resilience

**Never stop at:**
- "Because the component failed" (how did it fail?)
- "Because someone didn't know" (what would have told them?)
- "Because it was old" (why wasn't it upgraded? what would have triggered the upgrade?)
- "Because it's a known issue" (why wasn't it tracked and scheduled?)

## Multiple Chains

Most incidents have more than one independent failure chain. Common patterns:

**Compounding failures**: Chain 1 created the precondition that made Chain 2's impact much worse. Analyse them separately but note the relationship.

**Hidden pre-existing condition**: The incident triggered because a latent problem (stale IPAM blocks, corrupted image, expired cert) was finally hit. Two chains: the trigger and the pre-existing condition.

**Cascading failures**: Service A failed, causing Service B to fail, causing Service C to fail. Each service failure can be its own chain, but the root cause is often in the chain that explains why Service A failed AND why the failure propagated.

## Practical Tips for Homelab Incidents

**For watch stream stalls:**
- Don't stop at "the watch was stalled" — drill to what corrupted the watch source (kine/dqlite)
- Don't stop at "kine failed" — drill to why kine's reconnect logic failed to recover

**For network/VXLAN failures:**
- Don't stop at "routes were wrong" — drill to why Felix had stale state
- Explore whether the fix (manual `ip route replace`) should be automated

**For CrashLoopBackOff:**
- Don't stop at "the container kept crashing" — what is in the exit log?
- Drill through image pull failures, OOM kills, panic traces, signal handling

**For Pending pods:**
- Don't stop at "the pod was Pending" — was it scheduling, PVC binding, or kubelet?
- If kubelet: was the watch stalled, or was the node actually unschedulable?

## Example: Infinite How's Applied

**Symptom**: calico-node pod Pending for 35 minutes

```
How did calico-node-sqfcs sit Pending for 35 minutes?
→ The kubelet on k8s03 never received the pod CREATE event from the API server watch stream.

How did the kubelet fail to receive pod events from the watch stream?
→ The watch stream was silently stalled — TCP connection alive, HTTP/2 session alive,
  kubelet logged "Watching apiserver" — but no events flowed through it.

How does a live HTTP/2 connection deliver no events?
→ The API server watch cache sends events only when it receives them from kine.
  If kine is not delivering events, downstream watchers receive nothing, regardless
  of connection health.

How did kine stop delivering events to the watch cache?
→ kine failed to establish a watch against dqlite during the restart window.
  Logs showed: "WATCH Failed to create watcher: context canceled" and
  "database is locked" at 03:40–03:54 UTC.

How did dqlite become locked during kine's watch establishment?
→ dqlite uses SQLite for Raft log storage. During snapshot compaction or high
  concurrent write load, SQLite acquires an exclusive lock. kine's watch creation
  performs a compact-revision read, which landed during this lock window.

How was there no detection that the watch stream was stalled?
→ The kubelet logs "Watching apiserver" once at stream establishment and provides
  no health log if events stop. Absence of events looks identical to a quiet cluster.
  No NRPE check existed to compare scheduled pods vs. kubelet's pod list.
  PGM-201 (kine watch reliability) was filed but not yet resolved.

→ ACTIONABLE ROOT CAUSE: No monitoring for kubelet watch stream staleness.
  Action: NRPE check comparing API-server-scheduled pods vs /proxy/pods endpoint.
```

## Formatting in the PIR

Use H4 for chain headers and H5 for each question level:

```markdown
#### Chain 1: calico-node Stuck Pending — Kubelet Watch Stream Stall

##### How did calico-node-sqfcs sit Pending for 35 minutes?
...

##### How did the kubelet fail to receive events?
...
```

This creates a readable nested structure in MkDocs while keeping the drilling visible.
