# Budget enforcement is checked live and fails closed

ADR 0006 said an exhausted error budget switches Tier 2 off for its service, and ADR 0011 gave the allow-list a per-service suspension for it. This records how the switch is thrown.

`autonomy.py` asks for the budget state at the moment a command would otherwise be Tier 2, from the same recorded series `slo.py status` reads. Nothing writes the state into the allow-list. The alternatives were a scheduled job editing `disabled_services` by pull request, which leaves the switch waiting on a merge, or a bot-committed state file, which is stale by up to its schedule. A budget is most likely to run out during an incident, which is exactly when a stale switch does harm. The allow-list stays a file people review; `disabled_services` remains the manual override.

The check fails closed. A service governed by a budgeted SLO stays at Tier 1 when that budget is exhausted, when its state is unknown (no data), when the backend cannot be read or no token is available, and, for every service, when the site's SLO files do not validate. A service no budgeted SLO governs is never affected, and the backend is not queried for it.

The services a budget governs are its **freeze scope**: the journey's own service, plus those the journey names in `suspends:`. The catalog's dependency graph was rejected as the scope: it is partly unverified, and following it would let one exhausted leaf budget hold back the control-plane fix that would restore it. The owner writes the scope down.

The freeze on risky change is not machine-enforced. A machine cannot tell a risky change from a fix, and every Tier 1 mutation already asks a person. The freeze is made visible instead: in `slo.py status`, in the Nagios check's text, and in the blast-radius statement the agent must make, with the owner's exception recorded as a timeline `decision`.

## Consequences

- Wherever Tier 2 can run, so must a metrics read: the token and network path become part of Tier 2's preconditions.
- A metrics outage holds every governed service at Tier 1. That is the intended cost.
- Each refusal is a `tier2_suspended` audit record with its reason, so the review sees why an action was not taken.
