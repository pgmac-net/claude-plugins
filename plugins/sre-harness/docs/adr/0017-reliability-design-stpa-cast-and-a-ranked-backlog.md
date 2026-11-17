# Reliability design: targeted STPA, optional CAST, and a rule-ranked backlog

The incident history is dominated by detection gaps and remediation that did not land: self-heal scripts acting on a wrong belief, watchdogs nobody heard from, alerts hours after impact. Those are control problems, so the harness uses Leveson's control model rather than another failure-mode list.

**STPA is targeted, not a tier requirement.** A design review is one service, drafted by the agent from evidence (PIRs, runbooks, the source of every automated controller, the checks), with a person gating each of the four steps. It is not added to any tier's `requires`, because ten Tier 1 services each needing one would make it a box to tick. The recommendations backlog says when one is due: a Tier 1 service that keeps appearing in PIRs and has no analysis, an analysis three PIRs out of date, or proposals waiting from CAST.

**The analysis is data, not prose.** The front matter is schema-checked: every id resolves, every UCA cites evidence or says it is hypothesised, every uncontrolled scenario links an issue once reviewed. A diagram is generated from it. This makes a stale or dishonest analysis visible to `validate_site.py` rather than to a careful reader.

**Only uncontrolled scenarios become issues**, deduplicated against existing ones, with a `design-review` label the site creates with a fixed spec, like `incident`. Filing every scenario would bury the few that matter.

**CAST is optional in PIRs**, offered once when automation was involved, and it feeds proposals back into the service's analysis. Infinite How's stays the root-cause method.

**The backlog is ranked by rules, not by a model**, like the reliability report: the same PIRs, issues and catalog always give the same ranking, and the report stays reproducible. The backlog itself is the set of labelled issues; `trends.py` only ranks them. What it could not read (the PIR checkout, a private repository the token cannot see) is printed in the report, never silently dropped.

## Consequences

- PIRs carry a `services:` front-matter line so trends attribute them to catalog services; older PIRs are back-filled once, and tag matching is the fallback.
- A site that wants action items from private repositories in its report must give the report workflow a token that can read them. The first site uses a dedicated read-only GitHub App.
- Thresholds (theme size, window, staleness) are site settings with defaults, so other sites can tune them.
