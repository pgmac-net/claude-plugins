# Incidents can be driven from chat, with identity-bound approvals

A chat channel is a full control surface: a person can open an incident, ask for diagnostics and approve a Tier 1 mutation there. Broadcast-only chat was the safer alternative and was rejected because approving from a phone during an incident is the main reason to have chat at all.

Because chat can then restart production systems, approval is constrained. Only identities listed as approvers in site configuration count. The approval request states the exact command and its blast radius. An approval expires after a few minutes. The approver's identity and a link to the message are written to the audit log.

## Consequences

- The chat gateway is a thin bridge with no model logic, and connects outbound only.
- The incident issue stays the record of truth; chat threads are mirrored to it.
- A compromised chat account on the approver list can approve mutations. The list is kept short and changed only by reviewed pull request.
