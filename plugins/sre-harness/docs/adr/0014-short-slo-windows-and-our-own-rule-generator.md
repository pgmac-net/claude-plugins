# SLO windows fit the backend's retention; the harness generates its own rules

ADR 0005 put SLO definitions in OpenSLO files and made the backend evaluate them through generated recording rules. This records how the rules are generated, and why the windows are short.

An SLO window may not be longer than the backend keeps data. A hosted free tier keeps 14 days, recorded series included, so a 28- or 30-day window cannot be computed at all, and recording rules do not get around that. The first site uses a 7-day rolling window, well inside retention, so attainment survives a gap in ingestion. Longer views (a month, a quarter) come from daily snapshots kept in git, not from the backend.

The harness generates the rules itself (`slo.py generate`) rather than using Sloth or a vendor's SLO product. Sloth's OpenSLO support is the older alpha version and its defaults assume 28- and 30-day windows; a vendor product would hold the definitions outside the repository, which ADR 0005 rejected. Owning the generator means owning about three hundred lines and their tests; the window maths would have been customised either way.

A probe that stops reporting counts as failing: a gauge SLI's five-minute error ratio is 1 when there are no samples. Otherwise a dead prober, or a host outage that takes the prober with it, would make a journey look perfectly healthy. Within a window, the error is the share of reported probes that failed. The first version compared the number of samples with the number expected, and live data showed why not: scrape timing puts four or five one-minute samples in a five-minute window, and every window with four read as a 20% failure, spending budget on jitter. A request-ratio SLI with no traffic counts as having no errors, because no traffic is not evidence of failure.

Queries in the SLO files must be plain selectors; the generator adds the functions. That keeps the rules predictable and stops a definition file carrying arbitrary PromQL into the backend's ruler.

## Consequences

- Budgets are small: 99.5% over 7 days is 50 minutes. Targets are set where the service is today and reviewed after a few weeks of data.
- A host outage that takes out the prober spends the journey's budget even if the service was fine. The prober runs outside what it measures, and the policy says so.
- Every reader (the agent, a report, a person) gets attainment and budget from the same recorded series through `slo.py status`; no one writes budget maths by hand at a prompt.
- The rules are standard Prometheus recording rules in mimirtool's file format: any Prometheus or Mimir can load them.
