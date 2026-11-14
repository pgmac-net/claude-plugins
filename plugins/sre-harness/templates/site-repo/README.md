# Site repository

Site data for the `sre-harness` plugin: everything the harness knows about
one site.

| Path | Holds |
|---|---|
| `site.yaml` | Site configuration: repositories, monitoring sources, tracker, audit location |
| `tiers.yaml` | Service tier definitions |
| `catalog/` | Service catalog: generated Resources and hand-written services |
| `patterns/` | Deployment pattern pages |
| `autonomy.yaml` | Allow-list of pre-approved (Tier 2) actions. Empty by default |
| `.githooks/post-merge` | Optional: clears local incident state after a pull once the audit log has merged. Enable with `git config core.hooksPath .githooks` |
| `procedures/` | How incidents are worked here: monitoring access, evidence, comms, review format |
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
