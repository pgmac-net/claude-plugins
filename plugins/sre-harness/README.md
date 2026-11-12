# sre-harness

A portable harness for doing SRE work with an LLM agent: service catalog, incident triage and remediation, audit, user journeys and SLOs.

So far it has two parts: how a site is described and checked, and a service catalog kept in step with the site's own repositories. Incident and SLO skills arrive in later releases.

## How it is put together

The plugin holds logic and knows nothing about any site. Everything about one site lives in a private **site repository** that you own. See [`docs/adr/`](docs/adr/) for why, and [`CONTEXT.md`](CONTEXT.md) for the vocabulary.

## Set up a site

1. Copy [`templates/site-repo/`](templates/site-repo/) into a new **private** repository.
2. Edit `site.yaml` and `tiers.yaml`. Never put a secret in them: name the environment variable that holds it.
3. Point the harness at your checkout, one of:
   - `export SRE_HARNESS_SITE=/path/to/site-repo`
   - `ln -s /path/to/site-repo ~/.config/sre-harness/site`
4. Validate:

   ```bash
   uv run scripts/validate_site.py
   ```

   Or ask the agent to "check the site config" (the `site-check` skill).

## The catalog

The catalog is a set of entity files in Backstage format, in two layers that never share a file.

| Layer | Written by | Holds |
|---|---|---|
| `catalog/generated/` | `catalog_sync.py` only | One `Resource` per **deployment unit**: a host, an ArgoCD application, a monitored host |
| everything else under `catalog/` | People, or the `describe-service` skill | **Services** (`Component`), systems, owners: tier, purpose, dependencies, pattern, runbooks |

A service claims its deployment units by listing them under `dependsOn`.

**Sources.** `site.yaml` lists the sources of the generated layer. Each is read from a local git checkout, never from a live system, so sync works during an outage and always produces the same bytes from the same inputs.

| Source type | Reads | Produces |
|---|---|---|
| `ansible-inventory` | An INI inventory: hosts, groups, group children | A `host` Resource per host. Variables, `host_vars` and `group_vars` are never read |
| `argocd-applications` | A directory of areas; a Helm chart area is rendered with `helm template`, a plain one is read as YAML | An `argocd-application` Resource per `Application`, plus one for the cluster |
| `nagios-objects` | Nagios object files | Checks, host groups and parents merged onto the matching host or application; a `monitored-host` Resource for anything unmatched. Check commands are never recorded |

```bash
uv run scripts/catalog_sync.py            # rewrite the generated layer
uv run scripts/catalog_sync.py --check    # exit 1 if it is stale or hand-edited
uv run scripts/catalog_sync.py --check-live   # read-only: declared vs running ArgoCD applications
```

**Drift.** `catalog_check.py` needs only the site repository, so it runs in that repository's CI. `validate_site.py` runs it too.

| Finding | Level |
|---|---|
| Schema violation, duplicate entity, entity in the wrong layer | error |
| Reference to something that does not exist (dangling) | error |
| Service missing what its tier's `catalog_requires` lists | error; a warning if the item is under `sre.unverified` |
| Pattern with no page | error |
| Deployment unit no service reaches, not in `catalog/ignore.yaml` (uncatalogued) | warning |
| Fact drafted but not confirmed (`sre.unverified`), runbook file not found, stale ignore entry | warning |

Ask the agent to "sync the catalog" (`catalog-sync`) or "describe a service" (`describe-service`).

## Contents

| Path | Purpose |
|---|---|
| `skills/site-check/` | Finds and validates the site; shared reference for loading site configuration |
| `skills/catalog-sync/` | Regenerates the generated layer, reports drift, proposes a pull request |
| `skills/describe-service/` | Interviews the user and writes one service entry |
| `schemas/` | JSON Schema for `site.yaml` (version 2), tier definitions and catalog entities |
| `scripts/validate_site.py` | Site validation: schema, paths, references, audit privacy, secret shapes, catalog |
| `scripts/catalog_sync.py` | Source adapters and the generated layer |
| `scripts/catalog_check.py` | Entity validation and drift report |
| `scripts/check_identifiers.py` | Scans for site identifiers that must not ship in the plugin |
| `templates/site-repo/` | Starter layout for a new site repository |
| `docs/adr/` | Design decisions |
| `CONTEXT.md` | Glossary |

Scripts need [`uv`](https://docs.astral.sh/uv/); dependencies are declared inline. Catalog sync also needs `git`, and `helm` for chart areas.

## Upgrading a site from schema version 1

In `site.yaml`: set `schema_version: 2`, and replace `catalog_path: <dir>` with

```yaml
catalog:
  path: <dir>
  default_owner: user:<name>   # must exist under <dir>/owners/
  sources: []
```

Add `catalog_requires` to tiers in `tiers.yaml` to make the catalog check enforce what a service entry must state.

## Keeping site identifiers out

The plugin is public. CI runs `check_identifiers.py` with generic patterns (private addresses, home paths, internal hostnames, chat IDs, tokens). Before proposing a change, also scan against your own site's denylist, which never leaves your private repository:

```bash
uv run scripts/check_identifiers.py . --denylist "$SRE_HARNESS_SITE/site-identifiers.txt"
```

Mark a deliberate example with `identifier-allow` on the same line.

## Development

```bash
cd plugins/sre-harness
uv run --no-project --with pytest --with jsonschema --with pyyaml pytest
ruff check . && ruff format --check .
uv run --no-project --with pylint --with pytest --with jsonschema --with pyyaml pylint scripts tests
```
