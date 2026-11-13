# Tier 2 is an exact-match allow-list; the harness decides and executes

A mutation is pre-approved only if the site lists it as a command template in its private allow-list, and the command equals that template token for token, with each variable part passing a stated rule. A script makes that decision, and the same script executes the command, as an argument list with no shell. The agent asks and supplies evidence of the precondition; it never types a pre-approved command into a shell. The list is empty by default and changes only by reviewed pull request.

Commands and templates are confined to a small character set: letters, digits, a few punctuation marks and single spaces. A matcher that tokenised like a shell, then handed the string to a real shell, would disagree with it about quoting, tilde, globs, braces and escapes, and something other than what was matched could run. Refusing those characters and not using a shell removes the disagreement rather than chasing it.

We rejected flags in runbook prose, where the model would judge whether a step "is" the pre-approved one and the runbooks may be public. We rejected letting the agent choose fixes below some severity: the systems this was built for cascade when the wrong thing is restarted.

Nothing here can physically stop an agent running some other command, so the control has a second half. The audit log is checked after the fact for every mutating shell command with no approval record for that exact command, and the review must account for each.

## Consequences

- A pre-approved command cannot use quoting, pipes or expansion. An action that needs them is a script on the target, called by a plain command.
- The precondition of an action is words; only the agent can check it. A run must state the evidence, which is recorded.
- An approval record is the agent's statement that a person agreed. Binding approvals to an identity needs the chat gateway and is a later milestone.
- Mutation detection in the after-the-fact check is pattern-based, per simple command, and fails closed on anything too long to classify. It will still miss unusual commands, and says so.
- Each action has a per-incident limit, a switch, and its service can be suspended, so that an exhausted error budget can turn Tier 2 off for that service.
- A drill never runs a pre-approved action.
