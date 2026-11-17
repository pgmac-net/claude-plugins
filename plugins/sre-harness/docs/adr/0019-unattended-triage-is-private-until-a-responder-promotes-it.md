# An unattended triage is private until a responder promotes it

A site's incident tracker may be public. Until now every entry on it was written while a person watched the session. An unattended triage writes hostnames, evidence and a proposed fix with nobody reading first, and many triages will turn out to be noise.

**The triage's record opens in `runner.tracker_repo`**, which validation requires to be private, with only the `auto-triage` label. Not the site's incident label: in a site repository that label marks action items, and an unreviewed triage is not one.

**A responder decides what it was.**
- `incident.py promote --by <responder>` opens the incident on the site's tracker. It copies the triage's issue body and its `decision` and `summary` entries, marked "(from triage)", but not its raw `discovery` evidence. It closes the private issue with a link and rebinds the session to the new incident. The new incident's audit log begins with a `promoted_from` record naming the triage log and its head hash, so the two logs chain. `audit.py commit` of the promoted incident commits both logs on one branch.
- `incident.py dismiss --by <responder> --reason …` labels the private issue `not-an-incident` and closes it.

Both are refused in a triage-only session, so only a person's word in the channel makes either happen. A drill is never promoted.

We rejected opening on the public tracker straight away, as a person-run incident does. It is simpler, but it publishes text nobody has reviewed, and noise from flapping checks lands beside the real incidents.

## Consequences

- Promotion carries conclusions, not evidence. The full triage record stays private, and the PIR can link to it by id.
- An unattended triage cannot attach to an open incident: that would write unreviewed text into a record a person owns. It opens its own (`open --auto --new`) and names the related one in a discovery entry.
- The count of dismissed triages is a measure of runner noise, for a later report.
