# Other agents run the skills; the gateway stays on Claude Code for now

ADR 0002 chose skills, MCP and plain files partly so that agents other than Claude could run the same content. This records how far that goes today.

**Skills and scripts are agent-neutral.** Any agent that can read markdown and run a shell can work an incident: read the `SKILL.md`, run the scripts by path. `AGENTS.md` at the plugin root tells an agent without plugin support where things are and which rules hold everywhere. Pi also loads the skills natively, through the repository's `package.json`.

**Audit capture goes through the one hook implementation.** Claude Code calls `scripts/audit_hook.py` from its hooks. Pi calls the same script from a small extension (`adapters/pi/sre-audit.ts`). The extension maps Pi's tool events into Claude Code's hook payload shape and adds `agent` and `model`. Redaction, binding, hashing and chaining therefore exist once. Every record now names its agent, `claude-code` by default, and the model when the agent reports one. An agent with neither hooks nor extensions self-reports with `audit.py record --agent … --model …`; such a record is marked `self-reported` and counts as the weaker record it is.

**The chat gateway stays on Claude Code.** Chat approvals (ADR 0013) depend on a permission prompt that the gateway answers outside the model, bound to the tool-use id. The Claude Agent SDK gives that through `can_use_tool`. Pi has no permission system of its own. Its extension `tool_call` event can block a call, which is the likely base for a Pi driver behind the gateway's `AgentDriver` interface, but that needs:
- an approval wait inside the event;
- ids the gateway can bind to;
- a policy equivalent to the allow and deny rules.

That is its own piece of work, not a side effect of this one.

**Quality is the model's, and is documented, not assumed.** `docs/supported-agents.md` records, per agent and model, which steps of the alert drill worked: the supported floor.

## Consequences

- An unattended triage (ADR 0018) always runs on Claude Code until a Pi driver exists.
- Running the skills under Pi interactively is supported, but nothing stops a Pi session from mutating: Pi asks no one. Load a guard extension, or use Pi only for drills and read-only work, until the driver exists.
- MCP transport matters. Pi speaks only streamable-HTTP MCP, so a site whose monitoring MCP server offers only SSE leaves Pi on the site's documented fallbacks.
