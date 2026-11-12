# Skills, MCP servers and plain files; a runner later

Workflow logic is Agent Skills (markdown), tool access is MCP servers, and state is YAML, markdown and issues in git. We chose this over a standalone orchestrator service calling model APIs through a provider abstraction. Skills and MCP are open formats that several agents already run, they need no paid API access beyond an existing subscription, and they reuse the incident skills already in use.

A headless runner is still planned, but only for work that needs an always-on process: a chat gateway and alert-triggered triage. It will call the same skills rather than reimplement them.

## Considered options

- **Standalone orchestrator**: full control and unattended operation, but a large build and metered API cost from day one.
- **Skills only, forever**: cannot react to an alert or a chat message with nobody at a terminal.
