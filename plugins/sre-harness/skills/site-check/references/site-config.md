# Loading site configuration

Every sre-harness skill gets its site facts the same way. Link here instead of restating it.

## Finding the site repository

First match wins:

1. An explicit path the user gave.
2. The `SRE_HARNESS_SITE` environment variable.
3. `~/.config/sre-harness/site` (honours `XDG_CONFIG_HOME`): a symlink to the site repository, or a file whose first line is its path.

If none match, stop and say so. Never guess a path and never search the filesystem for a `site.yaml`.

## Reading it

Run the validator and use its JSON output; do not parse `site.yaml` by hand before it has been validated.

```bash
uv run <plugin root>/scripts/validate_site.py --json
```

The output gives `site_root`. After a valid result, read `<site_root>/site.yaml` for detail.

## What `site.yaml` holds

| Key | Meaning |
|---|---|
| `site` | Name and description |
| `repos` | Repositories by alias. `catalog` is the site repository itself. Each has `visibility`; others may have a local `path`, content paths and a `fallback_url` |
| `tracker` | Repository alias and label for incident issues |
| `monitoring` | Sources the agent may query: type, endpoint, MCP server name, names of credential environment variables |
| `tiers_file` | Service tier definitions |
| `catalog` | Catalog directory, default owner, runbooks repository, and the sources the generated layer is built from |
| `patterns_path` | Deployment pattern pages |
| `audit` | Repository alias and directory for audit logs. Must be private |
| `incident` | Time zone, severity criteria, status-update intervals, site procedures per stage, evidence directory, drill tracker, the pre-approved action list, and the optional advisory helper |
| `chat`, `slo` | Reserved; must be empty |

Schemas: `<plugin root>/schemas/site.schema.json` (version 2), `tiers.schema.json`, `entity.schema.json`. Unknown keys are errors.

## Rules

- Secrets never appear in `site.yaml`. `credentials_env` names environment variables; read the value from the environment at the moment of use and never print it.
- Use repository aliases, not hardcoded paths, in every skill.
- If a local checkout is missing or stale, use that repository's `fallback_url` and say the copy may be out of date.
