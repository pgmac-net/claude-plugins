# Budget history is kept as daily snapshots

The metrics backend keeps 14 days, recorded series included (ADR 0014), so a monthly report cannot be computed from it after the month ends. The harness writes one line per SLO per day to a history directory the site keeps in git, and builds the report from those lines.

A day's line is read at the day's local end: the 1-day error ratio for attainment, the lowest budget remaining and the highest 1-hour and 6-hour error ratios during the day for the worst state reached, and the number of minutes the rules actually evaluated. Monthly attainment is the mean of the daily values: exact for equal-length days, with no overlap between the 7-day windows.

Each run writes every missing day still inside retention, not only yesterday, so a missed scheduled run repairs itself from the same recorded series. Lines already written are never rewritten. Only an outage longer than retention loses data, and the report then says how many days are missing and fails its run.

The first site keeps the history on an orphan branch of its site repository, pushed by a scheduled workflow: the data is in git and private, the default branch gets no bot commits, and nobody merges a pull request a day. The harness only needs a directory; where it lives is the site's choice.

## Consequences

- A day read from a backfill is recomputed from the recorded series rather than observed at the time; since those series are what the live reading would have used, the values are the same. Lines say `"captured": "backfilled"`.
- A day the rules only partly covered (the first day, an ingestion gap) carries fewer `data_minutes`, and the report says how many days were partly measured rather than presenting them as whole.
- Snapshots record each SLO's freeze scope at the time, so the report counts frozen days as they were, even if `suspends:` changes later.
