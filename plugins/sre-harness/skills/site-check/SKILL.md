---
name: site-check
description: This skill should be used when the user says "check the site config", "validate site.yaml", "is the SRE harness set up", "site-check", or when another sre-harness skill fails to load its site. Finds the site repository, validates its configuration and reports what is configured and what is missing.
version: 0.3.0
---

# Site Check

Find the site repository, validate it, and report its state. Read-only: this skill never edits the site repository.

## Steps

1. **Run the validator.** It resolves the site by the rule in `references/site-config.md`; do not look for the site yourself.

   ```bash
   uv run <plugin root>/scripts/validate_site.py --json
   ```

   `<plugin root>` is two directories above this skill's base directory. Pass `--site <path>` only when the user names a path.

2. **Read the exit code.**

   | Exit | Meaning | Do |
   |---|---|---|
   | 0 | Valid | Report the summary and any warnings |
   | 1 | Invalid | Report each error with the fix; change nothing |
   | 2 | No site resolved | Explain the three ways to point at a site (`references/site-config.md`) |

3. **Report.** Lead with valid or invalid. Then: site name, repositories and their visibility, monitoring sources, tier names, audit location, incident settings (time zone, update intervals, procedures per stage, drill tracker), and the catalog: sources, services by tier, generated Resources, uncatalogued count, unverified services. Catalog findings are prefixed `catalog:`; for anything beyond reporting them, use the `catalog-sync` skill. List `chat` and `slo` as "not configured yet" when empty; that is expected until their milestones ship.

4. **Check staleness** when the site has catalog sources: `uv run <plugin root>/scripts/catalog_sync.py --check`. Exit 1 means the generated layer is behind its sources; say so and point at `catalog-sync`. Skip this step if a source checkout is missing.

## Gotchas

- A warning about a missing local checkout is not an error. The harness falls back to that repository's `fallback_url` when it has one.
- The validator never prints a value it thinks is a secret. Report the path of the offending key, and tell the user to move the value to an environment variable and rotate it if the file was ever committed.
- An audit location in a public repository is always an error. Do not suggest making an exception.
