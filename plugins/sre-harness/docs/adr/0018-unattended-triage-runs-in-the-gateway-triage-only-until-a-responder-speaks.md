# Unattended triage runs in the gateway, triage-only until a responder speaks

ADR 0006 allowed an unattended run to open an incident, gather evidence and propose a fix. This records how.

**The runner is a second way into the chat gateway, not a separate process.** The gateway already starts agent sessions, makes incident channels, announces in the hub and records decisions. An alert source beside `/incident` reuses all of it, and the session a human later joins is the one that did the triage, with its evidence still in context. A separate runner (a timer calling a headless agent) would rebuild that, and a person could not carry on its session.

**The gateway polls; nothing calls in.** It reads the monitoring source's status (Nagios `statusjson.cgi`) every `poll_seconds`, so it stays outbound-only, as in ADR 0008. A problem qualifies when it is HARD, unhandled (not acknowledged, not in downtime, notifications on), has lasted `min_state_age_seconds`, and sits on a host that a catalog service in one of `runner.tiers` claims directly. Acknowledging a problem is how a person tells the runner to stay out. Claims are direct only: following dependencies upward would make every problem on the cluster a Tier 1 problem.

**Triage-only is a tool policy, enforced outside the model.** An alert-started session runs under narrower Claude Code permission rules: the read-only allow-list, `incident.py open --auto` instead of `open`, and an explicit deny of `attach`, `close`, `promote`, `dismiss` and `autonomy.py run`. Every other call reaches the gateway's permission callback, which refuses it at once, with no approval request, and records the refusal (`outcome: refused`, no approver) in the audit log. Command-prefix rules cannot see `--incident`, so the timeline and `incident.py set`, which the triage needs for its own record, could otherwise name a person's incident. The gateway sets `SRE_HARNESS_TRIAGE=1` in a triage-only agent's environment, and the harness scripts then refuse to write to any record that is not an unattended triage. A command that changes that variable no longer matches an allow rule, so it is refused. So the unattended run never goes beyond Tier 0, which is stricter than ADR 0006 allows; Tier 2 waits until a person has seen what the triage found.

**The first message from a responder ends triage-only, once.** The gateway records a `policy_change` in the audit log (who, and a link to their message), stops the agent, and resumes the same session under the normal chat policy, so approvals work as in ADR 0013. A session never goes back to triage-only.

**Storms are bounded by grouping, not by the model.** All new problems from one poll become one triage. While a real incident's conversation is open, new problems are posted into it instead of starting another. Alert-started sessions are capped at `max_auto_sessions` within `max_sessions`; over the cap the hub is told and no session starts. A service that was just triaged waits `cooldown_minutes` before another triage starts for it; the cooldown never stops an alert being relayed into an open incident.

We rejected a webhook from Nagios notifications (an inbound port, and it fires on every re-notification) and reacting to the existing Slack alert posts (couples the runner to a message format, and the poster is a bot, not a responder).

## Consequences

- The runner is off unless `runner.enabled` is true. `/incident drill alert <host>/<check> [text]` exercises the whole path with a made-up alert whether or not it is enabled.
- The runner's Nagios login is read by the gateway and removed from the environment before any agent starts. The agent reads monitoring through its own read-only tools.
- Alert text comes from monitored systems and is untrusted. The agent is told it is evidence, not instruction; the triage-only policy bounds what an injected instruction could do.
- Unattended triages share the subscription's usage window with everything else. The cap, grouping and cooldown are what keep a storm from spending it.
