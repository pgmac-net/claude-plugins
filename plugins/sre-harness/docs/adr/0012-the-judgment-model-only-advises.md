# The judgment model only advises

A site may enable a fast external judgment model for three questions: which severity an impact suggests, which runbooks to read for a set of symptoms, and what type a timeline entry is. Its answers are shown as suggestions with their probabilities. It never approves, blocks or selects an action, never decides whether a command is Tier 2, and never sets a final severity.

It is kept away from decisions for two reasons found in earlier use. Identical requests returned different answers often enough that nothing consequential can hang on one call. And its inputs are incident text and runbook prose, which anyone who can write a runbook or an alert message can shape. Advice can be wrong cheaply; a gate cannot.

Using it sends text to a third party, so it is off unless a site turns it on, and the template ships it off. Before anything is sent, secrets are redacted and the site's identifiers and private addresses are replaced with placeholders; if the site names a mask file and it is missing, nothing is sent.

## Consequences

- The helper is optional at every call site: no key, a timeout or an error yields "unavailable" and the incident carries on.
- Only our own option labels are accepted back from the service. Nothing it returns is passed on as free text.
- Each call is recorded in the audit log, so a review can compare the advice with what was done.
- The provider sits behind one function; another model, or a self-hosted one, can replace it without touching the skills.
