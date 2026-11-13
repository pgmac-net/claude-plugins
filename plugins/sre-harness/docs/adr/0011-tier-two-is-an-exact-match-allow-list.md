# Tier 2 is an exact-match allow-list, decided by a script and checked afterwards

A mutation is pre-approved only if the site lists it as a command template in its private allow-list, and the command about to run equals that template token for token, with each variable part passing a stated rule. A script makes that decision; the agent asks it and acts on the answer. Anything with shell operators, an extra argument or a different target is Tier 1. The list is empty by default and changes only by reviewed pull request.

We rejected flags in runbook prose, where the model would judge whether a step "is" the pre-approved one and the runbooks may be public. We rejected letting the agent choose fixes below some severity: the systems this was built for cascade when the wrong thing is restarted.

Nothing here can physically stop an agent running a command, so the control has a second half. The audit log is checked after the fact for every mutating command that has neither an approval record nor a Tier 2 claim for that exact command, and the review must account for each.

## Consequences

- The precondition of an action is words; only the agent can check it. A claim must state the evidence, which is recorded.
- An approval record is the agent's statement that a person agreed. Binding approvals to an identity needs the chat gateway and is a later milestone.
- Mutation detection in the after-the-fact check is pattern-based and will miss unusual commands. The report says so.
- Each action has a per-incident limit, a switch, and its service can be suspended, so that an exhausted error budget can turn Tier 2 off for that service.
