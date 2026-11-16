---
name: define-journey
description: This skill should be used when the user says "define a journey", "write a user journey", "add an SLO", "set an SLO for <service>", "what should this service's SLI be", "write an error budget policy", or when a Tier 1 service is reported as having no journey. Interviews the product owner and writes a journey, its OpenSLO definitions and its error-budget policy beside the service's catalog entry, then generates the recording rules.
version: 0.1.0
---

# Define a Journey

Turn a conversation with the service's owner into three files in the site repository, and the rules that measure them. Site loading: `../site-check/references/site-config.md`. File formats and examples: `references/journey-files.md`.

A **journey** is what a user does with the service, end to end, in their words: "open an internal web app", not "nginx serves HTTP". An **SLI** measures one step as good events over all events. An **SLO** is a target for an SLI over a rolling window. The **error-budget policy** says what each budget state changes.

`<plugin root>` is two directories above this skill's base directory.

## Before anything else

1. Load the site. If `site.yaml` has no `slo` section, stop: SLOs need a metrics backend. Say what to add (the template's commented example) and why.
2. Read the service's catalog entry. Its tier says how much is required (`tiers.yaml`): Tier 1 needs journeys, SLIs, an SLO and a policy.
3. List the journeys it already has: `<journeys_path>/<service>/`.

## The interview

Ask one question at a time. Offer a concrete suggestion each time, drawn from the catalog, so the owner corrects rather than invents.

1. **The journey.** Who is the user, and what are they trying to get done? Name it as an action. Prefer the one most other things depend on.
2. **The steps.** The critical steps, in order, that must all work. For each, the catalog entries it depends on (`dependsOn` of the service and its neighbours). Every reference must exist in the catalog.
3. **The SLI per step.** Which signal shows the step worked, from what the backend already has (query it if you can). Prefer one SLI that covers every step, a synthetic probe walking the whole journey, over one per step. Say which steps an SLI does not cover, and why that is acceptable.
   - A **probe** (gauge, `counter: false`) counts missing samples as failures: use it for the SLI that spends budget.
   - A **request ratio** (counter, `counter: true`) cannot tell this service's failure from an application behind it. Record it with `sre-harness/budget: "false"` unless the service owns every response.
4. **The target and window.** The window must fit the backend's retention (`retention_days`); with 14 days, use 7d. Show the budget in minutes for each target you offer (window minutes × (1 − target)). Start where the service is today, not where it should be; say when to review it.
5. **The error-budget policy.** What healthy, burning fast and exhausted each change for this service. At minimum: exhausted freezes risky changes to the service and what it depends on, and puts reliability work first.

If a signal does not exist yet, write the files anyway and say what must be built for the SLI to have data. Do not invent a query that has never returned data.

## Writing it

Write, then check, then generate:

```bash
uv run <plugin root>/scripts/slo.py check
uv run <plugin root>/scripts/slo.py generate
```

`check` must pass with no errors. Commit the journey, its `.slo.yaml` files, the policy and the regenerated rules file together, on a branch, and propose them as a pull request.

After the rules are synced to the backend, confirm data arrives:

```bash
uv run <plugin root>/scripts/slo.py status --service component:<name>
```

`unknown` straight after a sync is normal; it should turn into a state within the shortest window.

## Gotchas

- Queries in `.slo.yaml` are plain selectors (`metric{labels}`). The harness adds `rate()` or `sum_over_time()` itself; a function in the query is refused.
- Keep SLI series few. On a free tier, every label value is a series: filter at the scraper so only what the SLI needs is shipped.
- A journey whose prober runs on the same host as the thing it measures cannot see that host fail. Probe from outside.
- The owner decides the target. You suggest and show the budget it implies.
