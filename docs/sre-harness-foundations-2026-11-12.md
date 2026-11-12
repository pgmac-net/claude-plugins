# sre-harness foundations (M0)

Ticket: pgmac-net/homelabia#234. Parent plan: pgmac-net/homelabia#223.

## What was built

A second plugin in this marketplace, `sre-harness` v0.1.0. It is the base
every later SRE harness milestone builds on:

- **Site configuration schema.** `schemas/site.schema.json` and
  `schemas/tiers.schema.json`, JSON Schema 2020-12, strict (unknown keys are
  errors), versioned with `schema_version: 1`.
- **Validator.** `scripts/validate_site.py`, run with `uv`. Beyond the schema
  it checks that repo aliases resolve, in-site paths exist, audit logs go to a
  private repository, and no value looks like a secret. Suspect values are
  never printed.
- **Identifier check.** `scripts/check_identifiers.py` scans for site
  identifiers with generic patterns (CI) and against a private denylist
  (local).
- **`site-check` skill.** Finds the site, runs the validator and reports. Its
  `references/site-config.md` is the shared "how to load site configuration"
  document for later skills.
- **Site repository template.** `templates/site-repo/`, using only
  documentation-reserved names.
- **Glossary and decisions.** `CONTEXT.md` and eight ADRs in `docs/adr/`.
- **CI.** `.github/workflows/sre-harness.yml`: ruff, pylint, tests, template
  validation, identifier check.

## Decisions

| Topic | Decision |
|---|---|
| Finding the site | Explicit path, then `SRE_HARNESS_SITE`, then `~/.config/sre-harness/site`, else stop. Never search |
| Schema scope | Fields needed up to the incident-core milestone. `chat` and `slo` are reserved and must be empty |
| Secrets | Never in `site.yaml`; referenced by environment variable name |
| Audit location | Must be a private repository; the validator refuses a public one |
| ADR placement | Harness design decisions here, public. Site decisions in the private site repository |
| Identifier guard | Generic patterns in public CI; real identifiers only in a private denylist |

## Deviations from the approved plan

- The denylist scan is a separate script (`check_identifiers.py --denylist`)
  rather than a `--check-leaks` flag on the validator. The validator needs
  third-party packages; the scanner is standard library only and also serves
  as the CI check.
- The OpenCode install scripts were not extended. They are written around one
  plugin's skill directory, and the PowerShell version could not be tested
  here. The README gives the manual symlink.
- The site repository's CI will pin this plugin by commit SHA, not by tag, so
  it does not wait on a release.

## Verification

- 44 tests pass; ruff and pylint (10.00/10) are clean.
- The validator accepts the template and rejects an unknown key, a
  secret-shaped value, a public audit location, an unknown repo alias and
  duplicate tier IDs.
- `check_identifiers.py` reports the plugin clean under the generic patterns.

## Not in this release

Catalog schema and sync, incident skills, audit hook, chat gateway, SLOs.
`start-incident` and `create-pir` stay in `pgmac-workflows` until the
incident-core milestone moves them.
