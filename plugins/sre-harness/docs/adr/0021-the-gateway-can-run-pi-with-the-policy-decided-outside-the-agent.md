# The gateway can run Pi, with the policy decided outside the agent

Supersedes the "gateway stays on Claude Code" part of ADR 0020.

ADR 0020 kept the chat gateway on Claude Code because chat approvals (ADR 0013) need a permission prompt that the gateway answers outside the model, bound to the call's id. Pi has no permission system. It does have an extension event, `tool_call`, that can block a call, and in RPC mode an extension can open a dialog that waits for the client's answer. Together those give what the driver needed:
- an approval wait inside the event;
- an id the gateway can bind to (`toolCallId`);
- a policy equivalent to the allow and deny rules.

**One agent per gateway, chosen in its environment.** `SRE_GATEWAY_AGENT=claude-code|pi`; Claude Code stays the default. `SRE_GATEWAY_MODEL` names the Pi model as `provider/id`. The choice covers chat incidents and unattended triage alike. It is not a site setting and not per incident: the site's policy does not depend on the agent.

**The policy is decided in the gateway, not in the agent.** A thin Pi extension, `adapters/pi/sre-policy.ts`, sends every tool call to the gateway as a blocking dialog and does what the answer says. It decides nothing itself, and it fails closed: no answer, a cancelled dialog or an unreadable answer blocks the call. The gateway judges the call with `scripts/tool_policy.py` against the same rule lists Claude Code gets (`chat_policy.rules()`, or `triage_rules()` in a triage-only session):
- **deny** refuses;
- **allow** runs;
- anything else becomes the existing approval request, or is refused and logged in a triage-only session.

**The matcher errs strict, and is stricter than Claude Code.** A shell command runs without asking only when it is one plain command that starts with an allow rule's prefix. It must have:
- no chaining, pipe, redirect or backgrounding;
- no `$` or backtick;
- no newline.

Deny rules are checked against the whole line and every simple command in it. Paths are resolved through symlinks. A Read deny rule also protects the path from editing. A recursive search asks first when its root could reach a denied file. Claude Code's built-in read-only commands are not allowed silently under Pi, so `cat` raises an approval request. Where Claude Code's own matcher is lenient, a call under Pi asks.

**Pi runs isolated, with only the harness.**
- **Home and config:** a gateway-owned home (`~/.config/sre-harness/pi`, set as `PI_CODING_AGENT_DIR`), so nothing from the operator's own `~/.pi` loads. It runs with no context files, no prompt templates and no project-local files, and with only the harness's skills.
- **Extensions:** the audit extension, then the policy extension.
- **Tools:** read, bash, grep, find, ls, edit and write, plus MCP tools exposed directly. Codemode and tool search are off, so every call the policy sees is one tool call, never a script of calls.
- **Chat input:** a chat message that starts with `/` is passed as text, never as a Pi command.

**Nagios through a local stdio server with the read-only login.** Pi cannot speak SSE MCP. The gateway writes a `nagios-mcp` stdio entry into the Pi home. Its config file holds the runner's read-only Nagios login, is mode 0600, and sits under `~/.config/sre-harness/`, which the deny-list already covers. The login never enters the agent's environment. Other stdio or HTTP servers from the gateway's MCP configuration carry over; SSE-only servers are left out with a warning.

## Consequences

- An unattended triage can run on Pi. The triage token, refusals and policy switch work as on Claude Code (ADR 0018).
- More calls raise approval requests under Pi than under Claude Code. That is the price of a matcher that refuses to guess.
- The provider login and its packages (for example `pi-claude-bridge`) are installed into the gateway's Pi home by the operator. A package's own extensions load there, so install only providers.
- Proven off production: unit tests, a stand-in Pi process speaking the RPC protocol, and an end-to-end drill on a workstation with the real gateway code and real `pi --mode rpc`. Production stays on Claude Code until a site switches it.
- Not covered: a per-incident agent choice, and falling back to Pi when Claude usage limits are hit.
