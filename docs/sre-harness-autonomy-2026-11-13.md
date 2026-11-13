# sre-harness autonomy tiers (M2, part 3 of 4)

Ticket: pgmac-net/homelabia#236. Builds on the incident skills and the audit log.

## What was built

`sre-harness` v0.5.0 adds pre-approved (Tier 2) actions.

- **`schemas/autonomy.schema.json`** — the allow-list: per action an id, a
  service, an exact command template, a rule for each `{placeholder}` (fixed
  values or a whole-value pattern), a precondition in words, a per-incident
  limit, and switches (site-wide, per action, per service).
- **`scripts/autonomy.py`** — `match` (which tier is this command?), `run`
  (match, require evidence of the precondition, record the claim, execute the
  command as an argument list with no shell, record the exit code), `list`.
- **`audit.py verify`** — now reports every mutating shell command that ran
  with no approval record for that exact command.
- **`validate_site.py`** — checks the allow-list: services exist in the
  catalog, placeholders and rules agree, no shell operators in a template.
- Skills: the agent asks the script before any mutation and never decides
  Tier 2 itself.

## How a match is decided

1. The site-wide switch is on.
2. The command uses only letters, digits, `. _ : / = @ , + -` and single
   spaces. No quoting, operators, or anything a shell would expand.
3. It has the same number of tokens as a template, and each token equals the
   template's, with placeholders taking a value.
4. Every placeholder value passes its rule.
5. The action is enabled, its service is not suspended, and its limit for
   this incident is not reached.

Anything else is Tier 1, with the reason. A Tier 2 command is then executed
by the script itself, without a shell. `run` refuses during a drill.

## Found in review

An automated security review of the first push raised three points, all valid.

| Finding | Change |
|---|---|
| The matcher tokenised with `shlex`, then the agent passed the string to bash, which reads `~`, globs, braces and escapes differently | Commands and templates are confined to a safe character set, and the harness executes the matched argument list itself with no shell |
| Any command mentioning a harness script was exempt from mutation detection, so a mutation could be chained to one | Commands are split at unquoted shell operators and each simple command is classified; only a segment that is itself a harness call is exempt; substitution removes any exemption |
| Classification looked only at the first 4,000 characters | Nothing is cut; a command too long to classify counts as a mutation |

A second review of those fixes raised three more, also valid.

| Finding | Change |
|---|---|
| The exemption accepted any `*/scripts/<name>.py` path, and any options after `uv run` | The script must resolve to a file in this plugin's own scripts directory, and no option may sit between the launcher and the script |
| Two `run`s at once could both pass the per-incident limit | One exclusive lock covers count, check and claim |
| The command was written unredacted into Tier 2 records | Redacted in both the claim and the result |
| (third review) A relative script path was resolved against the verifier's directory | Only an absolute path into this plugin's scripts directory is exempt |
| (third review) Tier 2 records skipped the credential variables the site names | They use the same redaction sources as the hook |

## Limits

- The script decides the match; it cannot stop an agent running something
  else. The after-the-fact report is the other half of the control.
- A precondition is words. The agent states its evidence; nothing checks it.
- An approval record is the agent's statement that a person agreed.
  Identity-bound approvals arrive with the chat gateway.
- Mutation detection is pattern-based: common commands, not every command.

## Verification

- 268 tests pass; ruff clean; pylint 10.00/10.
- Eight concurrent `run`s of an action limited to two: exactly two execute.
- Thirty-four near-matches of a listed command (extra flag, other namespace,
  `sudo`, chaining, substitution, redirection, quoting, tilde, globs, braces,
  escapes, extra whitespace, an environment prefix) are all Tier 1.
- `run` executes through an argument list: a chained `touch` never happens.
- The per-incident limit is enforced from the audit log.
