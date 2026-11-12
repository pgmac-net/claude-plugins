---
name: catalog-sync
description: This skill should be used when the user says "sync the catalog", "update the catalog", "regenerate the catalog", "is the catalog up to date", "catalog drift", or after changing hosts, ArgoCD applications or monitoring checks. Regenerates the generated layer of the site catalog from its source repositories, reports drift, and proposes the change as a pull request.
version: 0.2.0
---

# Catalog Sync

Bring the catalog's generated layer up to date with the site's source repositories, then report drift. Site loading: `../site-check/references/site-config.md`.

The generated layer (`<catalog>/generated/`) belongs to the script. Never edit a file in it, and never write a service entry for the user here: that is the `describe-service` skill.

## Steps

1. **Check staleness first.** `<plugin root>` is two directories above this skill's base directory.

   ```bash
   uv run <plugin root>/scripts/catalog_sync.py --check
   ```

   Exit 0: the generated layer is current; skip to step 5. Exit 1: it lists what would change. Exit 2: a source could not be read; report the message and stop.

2. **Update the source checkouts.** Sync reads local git checkouts, not remotes. For each source's repository run `git pull --ff-only`. If a pull fails, do not resolve it: say which checkout is behind and continue with what is on disk, noting the catalog may lag.

3. **Regenerate on a branch** of the site repository, never on its default branch.

   ```bash
   uv run <plugin root>/scripts/catalog_sync.py
   ```

4. **Read the diff before proposing it.** `git diff --stat` then the diff itself. Summarise in plain terms: deployment units added, removed, and changed (a chart version, a new check, a host that moved group). A removal is worth a sentence: something left the estate.

5. **Report drift.**

   ```bash
   uv run <plugin root>/scripts/catalog_check.py
   ```

   | Finding | Meaning | Offer |
   |---|---|---|
   | `dangling` (error) | A service points at something that no longer exists | Fix the service entry with the user |
   | `required at tier N but missing` (error) | A service lacks what its tier requires | `describe-service` to fill it |
   | `uncatalogued` (warning) | A deployment unit belongs to no service | Add it to a service, or to `ignore.yaml` with a reason |
   | `drafted but not confirmed` (warning) | A fact was drafted and nobody has confirmed it | Confirm with the user, then remove it from `unverified` |

6. **Open a pull request** on the site repository with the regenerated files and the summary from step 4. Do not merge it.

## Optional: compare with what is running

```bash
uv run <plugin root>/scripts/catalog_sync.py --check-live
```

Read-only. Lists ArgoCD applications declared but not running, and running but not declared. It needs `kubectl` and a `kube_context` on the source. Its output is a report; it never changes the catalog.

## Gotchas

- A warning about uncommitted changes means the checkout has local edits that are now in the catalog. Say so in the pull request, or stash them and re-run.
- "helm template failed" for an area means that area's applications are missing from this run, so they will show as removals. Do not propose that diff; fix the render first.
- Sync never reads live systems, inventory variables or check commands. If a fact is missing from a Resource for that reason, it is deliberate.
