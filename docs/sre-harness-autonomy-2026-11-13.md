# sre-harness autonomy tiers (M2, part 3 of 4)

Ticket: pgmac-net/homelabia#236. Builds on the incident skills and the audit log.

## What was built

`sre-harness` v0.5.0 adds pre-approved (Tier 2) actions.

- **`schemas/autonomy.schema.json`** — the allow-list: per action an id, a
  service, an exact command template, a rule for each `{placeholder}` (fixed
  values or a whole-value pattern), a precondition in words, a per-incident
  limit, and switches (site-wide, per action, per service).
- **`scripts/autonomy.py`** — `match` (which tier is this command?), `claim`
  (match, require evidence of the precondition, record it), `list`.
- **`audit.py verify`** — now reports every mutating command that ran with
  neither an approval record nor a Tier 2 claim for that exact command.
- **`validate_site.py`** — checks the allow-list: services exist in the
  catalog, placeholders and rules agree, no shell operators in a template.
- Skills: the agent asks the script before any mutation and never decides
  Tier 2 itself.

## How a match is decided

1. The site-wide switch is on.
2. The command is one simple command: no `;`, `&&`, `|`, redirection,
   substitution, variable or newline.
3. It has the same number of tokens as a template, and each token equals the
   template's, with placeholders taking a value.
4. Every placeholder value passes its rule.
5. The action is enabled, its service is not suspended, and its limit for
   this incident is not reached.

Anything else is Tier 1, with the reason.

## Limits

- The script decides the match; it cannot stop an agent running something
  else. The after-the-fact report is the other half of the control.
- A precondition is words. The agent states its evidence; nothing checks it.
- An approval record is the agent's statement that a person agreed.
  Identity-bound approvals arrive with the chat gateway.
- Mutation detection is pattern-based: common commands, not every command.

## Verification

- 221 tests pass; ruff clean; pylint 10.00/10.
- Twenty near-matches of a listed command (extra flag, other namespace,
  `sudo`, chaining, substitution, redirection, quoting tricks) are all Tier 1.
- The per-incident limit is enforced from the audit log.
