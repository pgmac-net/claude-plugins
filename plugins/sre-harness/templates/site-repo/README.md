# Site repository

Site data for the `sre-harness` plugin: everything the harness knows about
one site.

| Path | Holds |
|---|---|
| `site.yaml` | Site configuration: repositories, monitoring sources, tracker, audit location |
| `tiers.yaml` | Service tier definitions |
| `catalog/` | Service catalog entities |
| `audit/` | Per-incident audit logs |
| `docs/adr/` | Site decisions |
| `CONTEXT.md` | Site-specific terms |
| `site-identifiers.txt` | Real identifiers, for the local leak check |

## Use

Point the harness at this checkout, then validate:

```bash
export SRE_HARNESS_SITE=/path/to/this/checkout
uv run <plugin>/scripts/validate_site.py
```

Keep this repository private. It must not hold secrets: `site.yaml` names the
environment variables that hold them.
