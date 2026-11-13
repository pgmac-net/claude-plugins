# Pre-approved actions (Tier 2)

A site may list mutations that are safe enough to run during an incident without asking: typically one well-understood recovery step from a runbook, for one service. The list is the site's `incident.autonomy_file`. It is empty unless the site's owner added to it by reviewed pull request.

## The rule

Whether a command is Tier 2 is decided by a script, by exact match. Never by you, and never by how similar a command looks to a listed one. And a Tier 2 command is **run by the script**, not typed into a shell by you: that way what was matched is exactly what runs.

1. Ask, changing nothing:

   ```bash
   uv run <plugin root>/scripts/autonomy.py match --command "<the exact command>" --service component:<name>
   ```

2. If the answer is `"tier": 2`, read its `precondition` and confirm it from a read-only diagnostic you have run in this incident.

3. Run it through the script:

   ```bash
   uv run <plugin root>/scripts/autonomy.py run --command "<the same command>" --service component:<name> \
     --precondition-evidence "<what you observed, and how, that satisfies the precondition>"
   ```

   It records the claim in the audit log, executes the command as an argument list with no shell, records the exit code, and prints the command's output. Its own exit code is the command's; `10` means it was Tier 1 after all and nothing ran.

4. Write the `action` timeline entry with the observed effect, naming the action id.

If the answer is `"tier": 1`, treat it as any other mutation: state the blast radius, ask, record the approval, then act. Do not reshape the command to make it match. To read the list: `autonomy.py list`.

**Never run a pre-approved command in a shell yourself**, even after a Tier 2 answer. It would be executed, and the review would report it as a mutation with no authority, correctly.

## What makes a match

- Only letters, digits, `. _ : / = @ , + -` and single spaces. Quotes, `;`, `&&`, `|`, redirection, `$`, backticks, `~`, `*`, `?`, braces, brackets, backslashes, tabs and newlines all make it Tier 1.
- Token for token equal to the template. An extra flag, a missing flag, `sudo` in front, or an environment assignment in front is Tier 1.
- Every `{placeholder}` value passes its rule: one of a fixed set, or a whole-value pattern.
- The action is enabled, its service is not suspended, the site-wide switch is on, and the action has not reached its limit for this incident.

## The precondition is yours to prove

Each action states a precondition in words ("the pod is in CrashLoopBackOff and its volume is healthy"). The script cannot check it. You must have observed it with a read-only diagnostic in this incident, and say what you saw in `--precondition-evidence`. If you have not looked, look first. If it does not hold, the action is not available, whatever the match says.

## If it does not help

A Tier 2 action that did not fix the problem is information, not a reason to repeat it. The limit per incident exists for that. After it, you are in Tier 1 territory: stop and bring the person the evidence.

## During a drill

Mutate nothing. `run` refuses during a drill; use `match` to exercise the path.

## What the review will check

`audit.py verify` lists Tier 2 claims and their exit codes, and every mutating shell command that ran with no approval record for that exact command. Each of the latter has to be explained in the review. Its detection is pattern-based: it recognises common mutating commands, not every possible one.
