# Window SLIs for probes are failed over total, not an average of five-minute ratios

A probe SLO's window error ratio (1h, 6h, 1d and the SLO window) used to be the average of the recorded five-minute error ratios. This records why it is now failed probes over probes (claude-plugins#67).

**Averaging overlapping five-minute ratios overweights early failures.** The five-minute rule is evaluated every minute, so one probe sits in about five consecutive windows. In steady state each window holds about five samples and the weight comes out right. In a new SLO's first minutes the windows hold one to four samples, so one failed probe contributes about 1.0 + 0.5 + 0.33 + 0.25 + 0.2 of error across them, roughly 2.3 probes' worth against a small denominator. On 2026-11-23 one failed probe in the first minutes of the nfs-storage SLO kept it `exhausted`, and froze its scope, for hours while 115 raw samples showed 99.13% against a 99.0% target. A deliberate failure test minutes after the monitoring SLO's rules synced did the same.

**Each probe is counted once, by recording per-minute counts.** Two rules per probe SLO, evaluated every minute so that their one-minute ranges tile with no overlap: `slo:sli_total:count1m` (probes seen) and `slo:sli_bad:count1m` (probes that failed). A window's ratio is `sum_over_time(bad)` over `sum_over_time(total)`. The series names the status, dashboard and budget code read are unchanged. The five-minute ratio is unchanged too: it drives burn and the dashboards, and it was never the problem.

**Silence still counts as failing, a missed scrape does not.** When the probe has reported within the SLO's window but nothing arrived in the last five minutes, the minute records one failed probe. One empty minute from scrape jitter is not recorded at all. A probe that has never reported records nothing, so the state is `unknown` (claude-plugins#61).

**No minimum-evidence guard.** One failure in a new SLO's first ten samples is a real 10% error, and it reads exhausted until about a hundred samples are in a 99% window. That is the right arithmetic. Reporting `unknown` until the window fills was rejected: it would hide a probe that fails from the start, which is when the owner most wants to know, and `unknown` already holds Tier 2 at Tier 1. The `define-journey` skill says so instead.

**Rollout restarts the windows.** The new series exist only from the moment the regenerated rules sync, so each probe SLO's 1h to 7d windows start from zero and refill. An SLO that is really burning budget starts burning again within minutes, because the five-minute and burn series did not change. The old averaged history is not blended in: it carries the bias for up to a window, to avoid a one-time reset.

**Counter (request-ratio) SLOs are not changed here.** They average five-minute ratios, so a quiet hour weighs the same as a busy one. A one-minute `increase()` needs two samples and the scrape gives one, so the per-minute counts do not carry over: they need raw-window `increase()`. That is claude-plugins#68. The only counter SLO today is informational.

Verified by evaluating the generated rules with `promtool test rules` (`tests/test_slo_rules_promtool.py`), against fixtures for each case above.
