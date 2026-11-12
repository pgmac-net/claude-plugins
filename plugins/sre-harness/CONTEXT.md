# SRE harness

The portable vocabulary of the harness: the terms its skills, schemas and records use, independent of any one site.

## Language

### Sites and catalog

**Site**:
One operated environment the harness serves, described entirely by its site repository.
_Avoid_: Environment, tenant, installation

**Site repository**:
The private repository holding everything the harness knows about one site: configuration, catalog, tier definitions, audit logs and site decisions.
_Avoid_: Config repo, CMDB repo

**Site configuration**:
The `site.yaml` file at the root of the site repository.
_Avoid_: Settings, config file

**Catalog**:
The set of entity files describing a site's services, systems and resources and how they depend on each other.
_Avoid_: CMDB, inventory, registry

**Entity**:
One catalog record, in Backstage entity format: a Component, System, Resource or API.
_Avoid_: Item, asset, CI

**Service**:
Something a person or another service depends on, recorded as one hand-written Component entity.
_Avoid_: App, application, workload

**Deployment unit**:
One thing a source repository deploys or declares (a host, an ArgoCD application, a monitored host), recorded as one generated Resource entity.
_Avoid_: App, instance, workload

**System**:
A group of services with a shared purpose, recorded as a System entity.
_Avoid_: Area, domain, stack

**Source**:
A git checkout the generated layer is built from, read through one adapter.
_Avoid_: Feed, integration, provider

**Generated layer**:
The part of the catalog written only by sync and rewritten wholesale on every run.
_Avoid_: Auto catalog, discovered entities

**Drift**:
A mismatch between the generated layer and the hand-written entities: an uncatalogued deployment unit, a dangling reference, or a service missing what its tier requires.
_Avoid_: Skew, staleness

**Stale**:
Said of a generated layer that differs from what its sources would produce now.
_Avoid_: Drifted, out of date

**Uncatalogued**:
Said of a deployment unit that no service reaches and that is not deliberately ignored.
_Avoid_: Orphan, unowned

**Pattern**:
An agreed way a service is shipped at a site, described on one page and referred to by id.
_Avoid_: Template, blueprint, golden path

**Unverified**:
Said of a fact in a service entry that was drafted from evidence, or is not yet known, and that no person has confirmed.
_Avoid_: Draft, TODO, assumed

**Tier**:
A level of reliability commitment assigned to a service, which sets how much process the service gets.
_Avoid_: Priority, class, criticality level

### Reliability

**Journey**:
A path a user takes through a service to get something done, made of ordered critical steps.
_Avoid_: Flow, use case, user story

**SLI**:
A measured ratio of good events to all events for one journey step.
_Avoid_: Metric, KPI

**SLO**:
A target for an SLI over a rolling window.
_Avoid_: Target, goal, threshold

**SLA**:
A written commitment to a service's users, backed by a periodic report of SLO attainment.
_Avoid_: Contract, guarantee

**Error budget**:
The amount of unreliability an SLO permits over its window.
_Avoid_: Allowance, downtime budget

**Error-budget policy**:
The agreed consequences of each budget state: healthy, burning fast, exhausted.
_Avoid_: Freeze policy

### Incidents

**Incident**:
An unplanned loss or degradation of a service, tracked by one incident issue from opening to review.
_Avoid_: Outage, problem, event

**Severity**:
The impact rating of an incident, P1 (worst) to P4.
_Avoid_: Priority, tier

**Autonomy tier**:
The rule class of an action the agent may take: Tier 0 read-only, Tier 1 mutation confirmed by a person, Tier 2 pre-approved runbook step.
_Avoid_: Permission level, mode

**Mutation**:
Any action that changes the state of a system.
_Avoid_: Write, change, fix

**Blast radius**:
What a mutation can affect if it goes wrong, stated before approval is asked.
_Avoid_: Impact, risk

**Approver**:
A person whose identity is listed in site configuration as allowed to approve Tier 1 mutations.
_Avoid_: Admin, operator, owner

**Audit log**:
The machine-captured, append-only record of every command, tool call, approval and denial in an incident.
_Avoid_: History, transcript, command log

**Timeline**:
The curated, human-readable record of an incident, made of typed entries on the incident issue.
_Avoid_: Log, notes, journal

**Timeline entry**:
One timeline record with a type: discovery, milestone, update, handover, summary or decision.
_Avoid_: Comment, note

**Runbook**:
A documented procedure for one failure mode, with diagnosis separated from recovery.
_Avoid_: Playbook, SOP

**PIR**:
A post-incident review: the written analysis of an incident's causes and the actions that follow.
_Avoid_: Post-mortem, RCA document
