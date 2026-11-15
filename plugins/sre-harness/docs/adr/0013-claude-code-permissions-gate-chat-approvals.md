# Claude Code's permission system gates chat approvals; the gateway records them

ADR 0008 decided that chat can approve mutations, with identity-bound, expiring approvals. This records how.

The agent runs under Claude Code's own permission system, driven by the Claude Agent SDK. A tool call that matches the plugin's read-only allow-list (plus a site's reviewed additions) runs at once. A call that matches the deny-list is refused. Every other call stops at a permission prompt, and the gateway turns that prompt into an approval request in chat: the tool, its redacted input, and a link to the agent's statement of blast radius. Only an approver listed in site configuration can answer, checked against the platform's identity on the click itself. The request is single-use and expires. The gateway writes the decision to the audit log, then answers the prompt.

We rejected an approval flow inside the agent's own conversation ("reply yes to proceed"). The agent would be judging what counts as a yes and from whom; a permission prompt is answered outside the model, by code that checks the identity.

A chat approval names the one call it allowed, by the tool-use id Claude Code assigns, and the exact input. The audit hook records the attempt before the permission check runs, so the approval follows its attempt in the log, and verification credits an attempt only when the id and the redacted input both match. An approval cannot be reused for a second call or a changed command.

The agent cannot record an approval. The allow-list leaves out `audit.py record`, so calling it raises an approval request like any other unlisted command.

The SDK runs on the owner's subscription token rather than an API key, at the owner's decision on the terms. If that stops working, the gateway can drive the Claude Code CLI headless instead, behind the same driver interface.

## Consequences

- The allow-list is not a sandbox. Claude Code treats a fixed set of read-only shell commands as allowed in every mode, and applies path deny rules to them only on a best-effort basis. The deny-list narrows what the agent can read; the approval gate is what stops a change.
- A site may add allow rules only of a restricted form: a command prefix of at least two words with no shell characters, a path rule for Read, Grep or Glob, or an MCP server or tool. Whole tools, Edit and Write, wrapper commands, mutations and anything on the deny-list are refused by validation. A remote-shell prefix is accepted with a warning.
- Incidents driven from chat share the subscription's usage window with interactive work.
- The audit log is still a record kept by cooperating software on the same account. The gateway's records make chat approvals verifiable, not tamper-proof.
