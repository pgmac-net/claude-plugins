# sre-harness 0.22.0: a new probe SLO reads unknown, not exhausted (claude-plugins#61)

## Problem

A probe (gauge) SLO counted "no samples" as 100% error: its 5m rule ended in `or on() vector(1)`. The rules sync starts recording before the prober exists, so every minute until the first probe recorded 1. The window rules average only the samples that exist, so those few bad samples were the whole history: budget about -199, state `exhausted`, scope frozen with Tier 2 held, for hours after a healthy start. Seen on `dns-resolve-an-internal-name` on 2026-11-22 (sre-catalog#93, ansible#378); the deployment SLO showed it earlier.

## Change

`scripts/slo.py`, `error_ratio_5m`, gauge branch:

```
1 - avg(avg_over_time(<good>[5m]))
  or on() (vector(1) and on() (count(count_over_time(<good>[<window>])) > 0))
```

- A probe that has reported within the SLO's window and then goes silent still counts as failing (spends budget, can freeze).
- A probe that has never reported within the window records nothing. `slo.py status` returns `unknown` (existing path), Nagios reports UNKNOWN and Tier 2 stays held, but nothing is frozen and no budget is spent.
- A probe silent for the whole window also reads `unknown` (the lookback is the window). Still fails closed for Tier 2.
- Counter SLOs are unchanged.
- The `unknown` reason now names both causes: rules not synced, or the probe has not reported.

## Verification

- `tests/test_slo.py`: the exact expression, the lookback following the SLO window, the `unknown` reason. Full suite 808 passed; ruff clean; pylint 10.00 (exit 0).
- `promtool test rules` (prom/prometheus v2.55.1) against the generated group, 1m interval: never reported records nothing; healthy gives 0; reported for 10 minutes then silent gives 0 at minute 8 and 1 at minute 30; alternating 1/0 gives 0.5. All pass.
- Run against Grafana Cloud before planning: a never-reported selector returns nothing; the dns selector over 02:15-02:30 UTC was empty before the first probe and 0 after.

## Rollout

Plugin 0.22.0 changes the generated rules, so a site must regenerate (`slo.py generate`, `dashboard`) and sync. Recorded history is not rewritten: SLOs already exhausted at onboarding recover on their own as good samples accumulate.
