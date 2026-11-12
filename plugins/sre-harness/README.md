# sre-harness

A portable harness for doing SRE work with an LLM agent: service catalog, incident triage and remediation, audit, user journeys and SLOs.

This release is the foundation only. It defines how a site is described and checked. Incident, catalog and SLO skills arrive in later releases.

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

## Contents

| Path | Purpose |
|---|---|
| `skills/site-check/` | Finds and validates the site; shared reference for loading site configuration |
| `schemas/` | JSON Schema for `site.yaml` and `tiers.yaml` |
| `scripts/validate_site.py` | Schema validation plus path, reference, audit-privacy and secret checks |
| `scripts/check_identifiers.py` | Scans for site identifiers that must not ship in the plugin |
| `templates/site-repo/` | Starter layout for a new site repository |
| `docs/adr/` | Design decisions |
| `CONTEXT.md` | Glossary |

Scripts need [`uv`](https://docs.astral.sh/uv/); dependencies are declared inline.

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
