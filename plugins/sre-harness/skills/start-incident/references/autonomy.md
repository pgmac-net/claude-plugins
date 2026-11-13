# Pre-approved actions (Tier 2)

A site may list mutations that are safe enough to run during an incident without asking: typically one well-understood recovery step from a runbook, for one service. The list is the site's `incident.autonomy_file`. It is empty unless the site's owner added to it by reviewed pull request.

## The rule

Whether a command is Tier 2 is decided by a script, by exact match. Never by you, and never by how similar a command looks to a listed one.

```bash
uv run <plugin root>/scripts/autonomy.py claim \
  --command "<the exact command you intend to run>" \
  --service component:<the service being acted on> \
  --precondition-evidence "<what you observed, and how, that satisfies the precondition>"
```

| Answer | Do |
|---|---|
| `"tier": 2` | The claim is in the audit log. Run **that exact command, once**. Then write the `action` timeline entry with its effect, noting the action id |
| `"tier": 1` with a reason | Treat it as any other mutation: state the blast radius, ask, record the approval, then act. Do not adjust the command to make it match |

To look without claiming, use `match` in place of `claim`. To read the list, `autonomy.py list`.

## What makes a match

- One simple command. Anything with `;`, `&&`, `|`, redirection, `$(...)`, backticks, a variable or a newline is Tier 1.
- Token for token equal to the template. An extra flag, a missing flag, or `sudo` in front is Tier 1.
- Every `{placeholder}` value passes its rule: one of a fixed set, or a whole-value pattern.
- The action is enabled, its service is not suspended, the site-wide switch is on, and the action has not reached its limit for this incident.

## The precondition is yours to prove

Each action states a precondition in words ("the pod is in CrashLoopBackOff and its volume is healthy"). The script cannot check it. You must have observed it with a read-only diagnostic in this incident, and say what you saw in `--precondition-evidence`. If you have not looked, look first. If it does not hold, the action is not available, whatever the match says.

## If it does not help

A Tier 2 action that did not fix the problem is information, not a reason to repeat it. The limit per incident exists for that. After it, you are in Tier 1 territory: stop and bring the person the evidence.

## During a drill

Claim nothing and mutate nothing. Use `match` to exercise the path.

## What the review will check

`audit.py verify` lists every mutating command that ran with neither an approval record nor a Tier 2 claim for that exact command. Each one has to be explained in the review. Its detection is pattern-based: it recognises common mutating commands, not every possible one.
