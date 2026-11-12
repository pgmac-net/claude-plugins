---
name: describe-service
description: This skill should be used when the user says "describe a service", "add a service to the catalog", "catalogue this service", "write a catalog entry", "what tier is this service", or wants to fill in or confirm a service's dependencies, tier, pattern or runbooks. Interviews the user and writes one valid service entity in the site catalog.
version: 0.2.0
---

# Describe a Service

Turn a conversation into one valid service entry: a `Component` in `<catalog>/services/`. Site loading: `../site-check/references/site-config.md`. Field reference: `references/service-entry.md`.

A service is something a person or another service depends on. It is not a deployment unit: one service is often built from several generated Resources, and many Resources are not services at all.

## Steps

1. **Load the site and look before asking.** Read the tier definitions, the pattern pages, existing entries in `services/` and `systems/`, and the generated Resources that could belong to this service (search `generated/` by name, namespace, area and host). If the service already has an entry, this is an edit, not a new file.

2. **Interview, one question at a time**, each with your suggested answer from what step 1 found. Stop when every field below is settled or explicitly unknown.

   | Ask | Becomes |
   |---|---|
   | What do users lose when this is down? | `sre.purpose` |
   | Who depends on it: other services, or people directly? How bad is an outage? | `sre.tier`, checked against the tier definitions |
   | Which deployment units is it made of? (offer the Resources you found) | `dependsOn` → `resource:…` |
   | What must be working for it to work? (other services, storage, network) | `dependsOn` → `component:…` |
   | How is it shipped? (offer the site's patterns) | `sre.pattern` |
   | Which checks would tell you it is broken? | derived from its Resources; extras in `sre.monitoring` |
   | Which runbooks apply? (offer matches from the runbooks directory) | `sre.runbooks` |
   | Which system does it belong to? Who owns it? | `spec.system`, `spec.owner` |

3. **Do not invent.** A dependency the user did not confirm and you cannot show from the repositories is left out. A fact you drafted from evidence but the user has not confirmed goes in, with its field listed under `sre.unverified` and its source under `sre.evidence`. A required fact nobody knows yet is left empty and listed under `sre.unverified`.

4. **Write the file** `services/<name>.yaml` on a branch of the site repository. Reference a dependency on a service that has no entry yet only after asking whether to create that entry too; a dangling reference fails the check.

5. **Validate.**

   ```bash
   uv run <plugin root>/scripts/catalog_check.py
   ```

   Fix errors in the entry you wrote. Report warnings; `unverified` warnings are expected and are the list of things to confirm later.

6. **Show the entry and open a pull request.** Do not merge it.

## Gotchas

- Tier is a commitment, not a compliment. A higher tier requires more of the entry now and journeys and an SLO later. When the user hesitates between two tiers, describe what each requires and let them choose.
- Depend on the narrowest thing that is true. "Needs the cluster" is `resource:cluster-…`; "needs working ingress" is `component:ingress`. Prefer the service when one exists, so the dependency carries that service's tier and runbooks.
- Never write into `generated/`.
