# The advisory helper

Some sites enable a fast judgment model as a second opinion. It answers three narrow questions with a choice and probabilities. It is advice and nothing more.

## What it must never do

It never approves, blocks or selects an action. It never decides whether something is Tier 2: that is `autonomy.py`, by exact match. It never sets a final severity. If its answer and your reading of the evidence disagree, say so to the person and go with the evidence.

## When to ask

| Moment | Command | Use the answer to |
|---|---|---|
| Setting the provisional severity (phase 2) | `assist.py severity --impact "<what is broken, for whom>" --affected "<services>"` | Check your own reading. Report it as "the helper suggests P2 (0.71)" beside yours |
| Looking for a runbook (phase 3) | `assist.py runbooks --symptoms "<what is observed>"` | Decide which runbooks to **read** first. A ranking is not a match; a high `none_probability` is worth reporting as "no runbook seems to cover this" |
| Unsure of a timeline entry's type | `assist.py entry-type --text "<the entry>"` | Pick the type when you do not already know it |

```bash
uv run <plugin root>/scripts/assist.py severity --impact "…" --affected "…"
```

Each prints JSON. Ask once per question; do not re-ask to get a different answer. Its answers can vary between identical calls, which is one reason it only advises.

## When it is not there

`"available": false` with a reason means the site has not enabled it, there is no key, or the service did not answer in time. Carry on without it. Mention it once in the timeline if you had expected it; never wait for it or retry in a loop.

## What leaves the machine

Enabling the helper sends text to a third party. Before sending, the script redacts secrets and replaces the site's identifiers (from its mask file) and private addresses with placeholders. It refuses to send if the mask file is configured but missing. You still choose what goes in: give it the impact or the symptoms, not command output, credentials or anything about a person.

## In the record

Every call during an incident is written to the audit log as an `assist` record with its suggestion and confidence, so the review can see what advice was given and whether it was followed.
