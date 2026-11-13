# Skills say when; site procedures say how

The incident skills define phases, authority and record-keeping, and nothing about how any one site reaches its monitoring, captures evidence or publishes a review. That knowledge lives in the site repository as site procedures: prose written for the agent, listed per stage in `site.yaml`, and loaded when a skill reaches that stage. Where a skill and a procedure disagree on how, the procedure wins.

We rejected structured adapters per monitoring type with all access detail as configuration fields. The valuable knowledge is the gotchas and fallbacks ("this error means reconnect, not outage"; "if that host is down, this fallback is down too"), and those do not fit fields; every quirk would need a schema change. We rejected a private overlay plugin because it is a second thing to maintain and invisible to agents that do not load plugins.

## Consequences

- A site with no procedures still works: the skill falls back to querying each monitoring source by its type. It is simply worse.
- Procedures are unvalidated prose. The validator checks only that the listed files exist.
- Hard-won site knowledge stays out of the public plugin and beside the catalog it refers to.
