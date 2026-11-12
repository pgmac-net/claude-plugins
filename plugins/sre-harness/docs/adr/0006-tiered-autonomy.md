# Agent actions fall into three autonomy tiers

Tier 0 is read-only diagnostics, run freely. Tier 1 is any mutation: each one needs a person's approval, given after the blast radius is stated. Tier 2 is a runbook step explicitly marked safe to run without asking; it is opt-in per step, off by default and always logged. An unattended run may open an incident, gather evidence and propose a fix, but may not go beyond Tier 2.

We rejected "always confirm", which leaves an unattended agent able only to report, and "autonomous at low severity", where the model chooses its own fixes: on tightly coupled systems a wrong restart cascades.

## Consequences

- An exhausted error budget switches Tier 2 off for that service.
- Deterministic self-heal scripts are outside this model; they do not involve the agent.
