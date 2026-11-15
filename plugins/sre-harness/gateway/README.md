# sre-gateway

Chat-ops for the sre-harness plugin. People start, question and hand over an incident in Slack. An agent works it with the plugin's `start-incident` skill, and any command that isn't pre-approved waits for an approver's click.

The gateway is a thin bridge:
- It relays messages and turns the agent's permission prompts into approval requests.
- It records each approver's decision in the incident's audit log.
- It holds no model logic.
- It connects outbound only, over Slack Socket Mode, and opens no port.

Design: [ADR 0008](../docs/adr/0008-chat-ops-with-identity-bound-approvals.md) and [ADR 0013](../docs/adr/0013-claude-code-permissions-gate-chat-approvals.md).

## How it works

1. **Start.** A responder runs `/incident <what is broken>` (or `/incident drill <text>`), or mentions the bot in the hub channel. The gateway posts a line in the hub and starts an agent session in that message's thread.
2. **Open.** The agent opens the tracking issue (`incident.py open`). The gateway confirms the result against the harness's local state, creates a private channel `inc-<number>-<slug>` (`drill-…` for drills) and invites the responders. It continues there and announces the incident in the hub.
3. **Converse.** Each message from a responder is copied to the tracking issue as a comment, then sent to the agent. Anyone else's messages are ignored. Each agent turn finishes before the next message is sent; messages that arrive meanwhile are queued. `status` is answered by the gateway itself and never reaches the agent.
4. **Approve.** A tool call outside the allow-list stops at a permission prompt. The gateway posts an approval request in the channel. The request shows:
   - the tool and its redacted input
   - a link to the agent's statement of the command and its blast radius
   - Approve, Deny and Deny-with-reason buttons

   Only a listed approver can answer, checked against the user ID in each click. The request is single-use and expires (default 5 minutes). The decision is written to the audit log **before** the agent is answered; if it can't be written, the call is refused. No request can be made before the incident record exists.
5. **Resolve and archive.** When the agent runs `incident.py close`, the hub hears about it. Once a person closes the tracking issue, the channel is archived. The gateway checks hourly. The issue stays the record, which matters because Slack's free plan keeps 90 days.

**Sessions.** One resumable session per incident. A session stops after `idle_stop_minutes` and resumes by its ID on the next message. At most `max_sessions` run at once; others wait, and say so. The conversation-to-session mapping survives restarts (`~/.local/state/sre-harness/gateway/conversations.json`, mode 0600). Usage limits, crashes and restarts are posted in the channel.

## What the agent may do

The agent runs with only the plugin's chat policy (`scripts/chat_policy.py`). User, project and local Claude Code settings are not loaded.

| Without asking | Refused outright | Everything else |
|---|---|---|
| Read, Grep, Glob, skills; the harness scripts; `kube_read.py` (read-only kubectl, checked per argument); `gh issue/pr view/list`; `systemctl status`; site additions from `chat.allow` | Credential paths, the gateway's secrets, the harness state directory, `/proc`, `.env` files, `env`/`printenv`, `gh auth`, kubectl secrets/exec/port-forward/connection options, web fetch and search | An approval request |

The allow-list is not a sandbox. Claude Code itself lets a fixed set of read-only shell commands run in every mode, and applies path deny rules to them only on a best-effort basis. The approval gate is what stops a change.

## Install

Needs `uv`, the `gh` CLI signed in, `kubectl` if the agent should read a cluster, and a checkout of the site repository. The Claude Code CLI comes bundled with the Agent SDK.

1. **The Slack app.** Create it from [`slack-app-manifest.yaml`](slack-app-manifest.yaml) and install it to the workspace. Create an app-level token with `connections:write`. Invite the bot to the hub channel.
2. **The Claude token.** Run `claude setup-token` on a machine where you are signed in to Claude.
3. **Site configuration.** Fill in `chat` in `site.yaml` (schema v4) with the hub channel ID and the responder and approver user IDs, and run `validate_site.py`.
4. **Environment file.** Create `~/.config/sre-harness/gateway.env`, mode 0600:

   ```
   SLACK_BOT_TOKEN=…
   SLACK_APP_TOKEN=…
   CLAUDE_CODE_OAUTH_TOKEN=…
   SRE_HARNESS_SITE=/path/to/site-repo
   # optional: SRE_GATEWAY_MCP_CONFIG=/path/to/mcp.json   (monitoring MCP servers for the agent)
   # optional: SRE_GATEWAY_MODEL=<model>
   ```

5. **Install and check.**

   ```bash
   cd <plugin>/gateway
   uv sync --frozen --no-dev
   set -a; . ~/.config/sre-harness/gateway.env; set +a
   uv run --frozen --no-dev sre-gateway --check
   ```

6. **Run it as a user service.** Copy [`systemd/sre-gateway.service`](systemd/sre-gateway.service) to `~/.config/systemd/user/`, replace `@PLUGIN@`, and run:

   ```bash
   loginctl enable-linger "$USER"
   systemctl --user daemon-reload && systemctl --user enable --now sre-gateway
   ```

Upgrade by moving the plugin checkout to a new tag or commit, running `uv sync --frozen --no-dev`, and restarting the service.

## Operating it

- **Logs:** `journalctl --user -u sre-gateway`; add `-v` to `ExecStart` for the agent's stderr.
- **Check config without connecting:** `sre-gateway --check`.
- **Changing approvers:** edit `chat.approvers` by pull request, then restart. The list is read at start.
- **Renewing the Claude token:** replace `CLAUDE_CODE_OAUTH_TOKEN` and restart. Sign-in failures are posted in the channel.

## Threat model

| Threat | Control | What is left |
|---|---|---|
| A non-approver approves | Approver checked on each click against the platform's user ID; request stays open | A compromised approver account can approve. Keep the list short and change it by PR |
| An approval reused or replayed | Single-use requests; the audit approval names one `tool_use_id` and its exact input; verify credits only that call | — |
| Approval with no record | Decision written before the answer; a failed write refuses the call | The log is kept by cooperating software on the same account, not tamper-proof |
| Messages from outsiders steer the agent | Only listed responders reach it; channels are private | A responder can paste text that tries to instruct the agent. The approval gate and deny-list bound what follows |
| The agent reads or leaks a credential | Deny-list; the gateway's Slack tokens are removed from its environment before the agent starts; every post is redacted | The Claude token must stay in the agent's environment. Claude Code's own read-only commands can reach files the deny-list names, on a best-effort basis |
| An inbound attack surface | Socket Mode: outbound websocket only | — |
| Shared usage limits | Limit hits are posted; sessions are capped | Incidents compete with interactive use of the same subscription |

## Development

```bash
cd plugins/sre-harness/gateway
uv sync --group dev
uv run pytest -q
uv run pylint src tests
uvx ruff check . && uvx ruff format --check .
```

Tests use an in-memory chat platform, agent and tracker (`tests/fakes.py`). A new chat platform implements `sre_gateway.chat.ChatAdapter`.
