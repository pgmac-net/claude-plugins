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

**Budget state**:
Where an SLO's error budget stands now: healthy, burning fast (spending faster than the window allows), exhausted (none left), or unknown (no data).
_Avoid_: Health, status colour

**Freeze scope**:
The services an SLO's exhausted budget freezes: its journey's service, plus those the journey lists in `suspends`.
_Avoid_: Blast radius, dependency set

**Freeze**:
The state of a service in an exhausted budget's freeze scope: risky change waits for the owner's recorded exception, and its pre-approved actions are held at Tier 1.
_Avoid_: Lock, change freeze window

**Budget suspension**:
A pre-approved action held at Tier 1 because an error budget governing its service is exhausted, unknown or unreadable. Decided live, recorded in the audit log.
_Avoid_: Disabled action, Tier 2 off

**Snapshot**:
One line recording an SLO's day: attainment, lowest budget, highest burn, worst state and minutes measured. Kept outside the metrics backend, which forgets.
_Avoid_: Sample, export, backup

**Reliability report**:
The monthly report of attainment, budget and incidents for every service whose tier requires an SLA report. Merging it is the owner's acceptance.
_Avoid_: SLA report (the commitment is the SLA; this is its evidence), dashboard

**Canary**:
A minimal backend that exists only to be probed, so a probe's failure means the path to it failed, not an application.
_Avoid_: Test app, dummy service

**Probe**:
A synthetic request made on a schedule from outside what it measures; its success ratio is an SLI. A probe that stops reporting counts as failing.
_Avoid_: Ping, health check

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

**Advisory helper**:
An optional judgment model that suggests a severity, ranks runbooks to read and types timeline entries, and decides nothing.
_Avoid_: Assistant, judge, classifier

**Pre-approved action**:
A mutation listed in a site's allow-list as an exact command template for one service, runnable without asking when its precondition holds.
_Avoid_: Safe action, auto-fix, auto-remediation

**Claim**:
The recorded statement that a command matched a pre-approved action and that its precondition was observed.
_Avoid_: Use, invocation

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

**Capture**:
How an audit record came to exist: by the hook, or self-reported by an agent that has none.
_Avoid_: Source, origin

**Timeline**:
The curated, human-readable record of an incident, made of typed entries on the incident issue.
_Avoid_: Log, notes, journal

**Timeline entry**:
One timeline record with a type: discovery, action, decision, milestone, update, handover or summary.
_Avoid_: Comment, note

**Site procedure**:
A document in the site repository, written for the agent, saying how something is done at that site; the harness's skills say when it is read.
_Avoid_: Playbook, site notes, local docs

**Tracking issue**:
The one issue that is an incident's record: stable facts in its body, the timeline in its comments.
_Avoid_: Ticket, hold-all, incident page

**Incident id**:
The repository alias and issue number of the tracking issue, written `<alias>-<number>`.
_Avoid_: Incident number, reference

**Status update**:
A timeline entry of type update, owed at an interval the site sets per severity.
_Avoid_: Heartbeat, check-in

**Handover**:
A timeline entry that passes an incident to someone else, with five fixed fields.
_Avoid_: Handoff note

**Drill**:
A practice incident, worked end to end without mutations and tracked in a private repository.
_Avoid_: Test incident, game day, simulation

**Runbook**:
A documented procedure for one failure mode, with diagnosis separated from recovery.
_Avoid_: Playbook, SOP

**Chat gateway**:
The process that connects a chat platform to an agent session: it relays messages and asks for approvals, and holds no model logic.
_Avoid_: Bot, chat bridge

**Responder**:
A chat user whose messages reach the agent, listed by ID in site configuration.
_Avoid_: Member, participant

**Approver**:
A responder whose answer to an approval request counts.
_Avoid_: Admin, owner

**Approval request**:
A single-use, expiring chat message asking an approver to allow one tool call, stating the call and its blast radius.
_Avoid_: Approval card, confirmation prompt

**PIR**:
A post-incident review: the written analysis of an incident's causes and the actions that follow.
_Avoid_: Post-mortem, RCA document

### Unattended triage

**Alert source**:
The monitoring system the chat gateway polls for problems, named by `runner.source`.
_Avoid_: Feed, webhook, alert stream

**Qualifying problem**:
An unhandled HARD problem, old enough, on a host that a service in one of the runner's tiers claims directly.
_Avoid_: Alert (any problem), page

**Unattended triage**:
An incident started by the alert source with no person watching: it opens a private record, gathers evidence and proposes a fix, and changes nothing.
_Avoid_: Auto-remediation, bot incident, autopilot

**Triage-only policy**:
The tool policy of an unattended triage: read-only, plus opening its own private record and writing its timeline. Every other call is refused without asking anyone.
_Avoid_: Read-only mode, safe mode

**Policy change**:
The one-way move of a session from the triage-only policy to the chat policy, made when a responder first speaks, and recorded in the audit log.
_Avoid_: Handover (that is a timeline entry), escalation

**Promotion**:
A responder's decision that an unattended triage is a real incident: its conclusions move to the site's tracker and its audit log chains to the new one.
_Avoid_: Escalation, publishing

**Dismissal**:
A responder's decision that an unattended triage was not an incident; its private record is closed as `not-an-incident`.
_Avoid_: False positive (a property of the check, not the decision)

### Reliability design

**Design review**:
A targeted STPA analysis of one service's control structure, drafted from evidence and gated by a person at every step. Recorded as one analysis file per service in the site repository.
_Avoid_: Architecture review, audit

**Control structure**:
The controllers that act on a service, the processes they act on, the control actions between them and the feedback that comes back.
_Avoid_: Architecture diagram, topology

**Controller**:
Anything that decides and acts on a process: a person, an automated handler, or software. Automated controllers act with no person in the loop.
_Avoid_: Operator (only one kind of controller), agent

**Control action**:
One thing a controller can do to a process or to another controller.
_Avoid_: Command, operation

**Feedback**:
What tells a controller the state of what it controls. Present, inadequate or missing.
_Avoid_: Monitoring (one source of feedback), telemetry

**Process model**:
What a controller believes about the process it controls. Most unsafe actions come from a process model that is wrong.
_Avoid_: Assumption, mental model

**Unsafe control action (UCA)**:
A control action that, in a stated context, leads to a hazard: not provided, provided unsafely, at the wrong time or order, or for the wrong duration.
_Avoid_: Bad action, mistake

**Loss scenario**:
How an unsafe control action could come about, by its causal factors. Controlled when a working control handles it today; uncontrolled otherwise.
_Avoid_: Failure mode, risk

**Design-review finding**:
An uncontrolled loss scenario, tracked by one issue with the `design-review` label.
_Avoid_: Recommendation (that is the backlog's word), defect

**CAST**:
The STPA control model applied to one incident in a PIR: per controller, what it did, what it believed and what it was told. Optional, beside Infinite How's.
_Avoid_: Root cause analysis (Infinite How's is the RCA)

**Recurring theme**:
A service or PIR tag that appears on at least the site's threshold of PIRs inside the rolling window.
_Avoid_: Trend, pattern (a pattern is a deployment pattern)

**Recommendations backlog**:
The open action items (issues labelled `incident` or `design-review`), ranked by rules from PIR trends, with the design reviews that are due. Shown in the reliability report.
_Avoid_: Wishlist, roadmap
