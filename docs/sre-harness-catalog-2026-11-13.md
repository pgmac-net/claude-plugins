# sre-harness catalog (M1)

Ticket: pgmac-net/homelabia#235. Parent plan: pgmac-net/homelabia#223.

## What was built

`sre-harness` v0.2.0 adds a service catalog kept in step with a site's own
repositories.

- **Two layers.** `catalog/generated/` holds one Resource per deployment unit
  and is rewritten wholesale by sync. Services, systems and owners are written
  by people elsewhere under `catalog/`. A service claims its deployment units
  by reference.
- **`scripts/catalog_sync.py`.** Three adapters reading local git checkouts:
  Ansible INI inventory, ArgoCD `Application` manifests (Helm chart areas are
  rendered with `helm template`), Nagios object files. Deterministic output.
  `--check` reports a stale or hand-edited generated layer; `--check-live`
  compares declared and running ArgoCD applications, read-only.
- **`scripts/catalog_check.py`.** Entity schema validation, reference
  resolution and the drift report. Needs only the site repository, so it runs
  in that repository's CI. `validate_site.py` calls it.
- **Schemas.** `site.yaml` moves to `schema_version: 2` (a `catalog` section
  with sources replaces `catalog_path`; `patterns_path` added). Tiers gain
  `catalog_requires`. New `entity.schema.json`.
- **Skills.** `catalog-sync` and `describe-service`; `site-check` reports the
  catalog.
- **Glossary and ADR 0009** (the two-layer catalog).

## Decisions

| Topic | Decision |
|---|---|
| Sync source | Git checkouts, not live systems. Works during an outage; reproducible |
| Layers | Generated and hand-written entities never share a file. Drift is a mismatch between them |
| Model | Deployment unit = generated Resource. Service = hand-written Component. System groups services |
| Tier requirements | Each tier lists what a service entry must state (`catalog_requires`). `unverified` downgrades a missing item to a warning |
| Where it runs | Sync locally, proposing a pull request. Check in the site repository's CI |

## Safety choices

- The Ansible adapter parses the inventory itself and never opens `host_vars`
  or `group_vars`, and ignores variables on host lines. Running
  `ansible-inventory` would load them, and they can hold vault data.
- The Nagios adapter never records `check_command`, which can carry
  credentials.
- Sync writes only inside `generated/`.

## Deviations from the approved plan

- No `ansible-inventory` call, for the reason above. Host ranges
  (`web[01:03]`) are not expanded; they are reported and skipped.
- `--check-live` covers ArgoCD only. Nagios has no command-line read path
  here; the agent can compare through the Nagios MCP server.
- Staleness is detected by regenerating and comparing, not by comparing
  recorded commits. It also catches hand edits. Source commits are still
  recorded in `generated/sources.yaml`.

## Verification

- 75 tests pass; ruff clean; pylint 10.00/10.
- Run twice against real sources: second run changes nothing.
- Identifier scan clean, generic and against a private denylist.

## Not in this release

Scheduled sync in CI, incident skills, audit hook, chat gateway, SLOs.
Designing golden-path patterns is pgmac-net/homelabia#244.
