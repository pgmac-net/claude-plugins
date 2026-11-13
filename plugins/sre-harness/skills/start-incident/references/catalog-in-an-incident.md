# Using the catalog in an incident

The catalog turns "this host is alerting" into "these services are affected, they matter this much, and here is what to read". It lives under `catalog.path` in the site repository. Schema and layout: `../../describe-service/references/service-entry.md`.

## From a symptom to services

1. Find the generated Resource for the failing thing: `generated/**/host-<name>.yaml`, `app-<name>.yaml`, `monitored-<name>.yaml`. Its `spec.sre.monitoring.checks` lists what is watched on it; `dependsOn` lists what it sits on.
2. Find the services that claim it: search `services/` for its ref (`resource:<name>`) under `dependsOn`.
3. Follow dependencies **upwards**: which other services list those services under `dependsOn`? That is the blast radius, and the reason a small fault can be a large incident.

A Resource no service claims is uncatalogued. Say so in a `discovery` entry; it is a finding for the review, not a reason to assume it is unimportant.

## What a service entry gives you

| Field | Use it to |
|---|---|
| `sre.tier` | Weigh severity, and find the update interval the site expects |
| `sre.purpose` | Write the Impact line in the users' terms |
| `dependsOn` | Look for the cause below, and for collateral above |
| `sre.runbooks` | Start the runbook match here |
| `sre.pattern` | Know how a fix would ship, and how it is rolled back: read the pattern page |
| `sre.notes` | Read first. It holds what has bitten before |
| `sre.unverified` | Treat those facts as hints, not truth, and say when an incident proves or disproves one |

## When the catalog is wrong

An incident is the best test the catalog gets. If a dependency turns out to exist that the catalog lacks, or one it lists is false, record it as a `discovery` and raise it in the review. Do not edit the catalog mid-incident.
