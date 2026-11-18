<img src="../assets/icon-128.png" alt="SRE harness icon" width="96" align="right">

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

**Sessions.** One resumable session per incident. A session stops after `idle_stop_minutes` and resumes by its ID on the next message. At most `max_sessions` run at once; others wait, and say so. The conversation-to-session mapping survives restarts (`~/.local/state/sre-harness/gateway/conversations.json`, mode 0600). Usage limits, crashes and restarts are posted in the channel. If the agent fails to start, the gateway tries 3 times, 30s then 60s apart. After that, the message is kept and goes to the agent ahead of the next one, so an alert is never lost to a failed start.

## Unattended triage

With a `runner` section in `site.yaml` and `enabled: true`, the gateway also starts incidents itself. Design: [ADR 0018](../docs/adr/0018-unattended-triage-runs-in-the-gateway-triage-only-until-a-responder-speaks.md) and [ADR 0019](../docs/adr/0019-unattended-triage-is-private-until-a-responder-promotes-it.md).

1. **Poll.** Every `poll_seconds` it reads Nagios `statusjson.cgi` with a read-only login. A problem qualifies when it is HARD, not acknowledged, not in downtime, has notifications on, has lasted `min_state_age_seconds`, and is on a host that a catalog service in `tiers` claims directly. Acknowledge a problem to keep the runner out of it.
2. **Start.** All new qualifying problems from one poll start one triage, announced in the hub. The agent runs under the **triage-only** policy: it may read, open a record with `incident.py open --auto` (in the private `tracker_repo`) and write its timeline. Every other call is refused at once, with no approval request, and the refusal goes in the audit log. It ends with a summary: evidence, likely cause, and a proposed fix with blast radius and rollback.
3. **Join.** The first message from a responder in the channel ends triage-only for that session, for good. The gateway records the change in the audit log and restarts the agent under the normal chat policy. From then on approvals work as above.
4. **Decide.** The responder says whether it is real. The agent then runs `incident.py promote`, which moves the incident to the public tracker with the triage's conclusions and moves the channel with it, or `incident.py dismiss`, which closes it as not-an-incident.

**Limits.**
- While a real incident's conversation is open, new problems are posted into it instead of starting another.
- Only one unattended triage is worked at a time, because new alerts join the open one. Drills, and incidents already closed or dismissed, do not count. A closed incident's channel is archived within a minute of a restart, then hourly.
- After a triage, a service waits `cooldown_minutes` before it can start another.
- If the source can't be read, the hub is told once, and again when it recovers.

**Drill it** with `/incident drill alert <host>/<check> [what to pretend]`. This starts the same triage-only path with a made-up alert, whether or not the runner is enabled. Check what would start right now with `uv run <plugin>/scripts/alerts.py check --verbose`.

## What the agent may do

The agent runs with only the plugin's chat policy (`scripts/chat_policy.py`). User, project and local Claude Code settings are not loaded.

| Without asking | Refused outright | Everything else |
|---|---|---|
| Read, Grep, Glob, skills; the harness scripts; `kube_read.py` (read-only kubectl, checked per argument); `gh issue/pr view/list`; `systemctl status`; site additions from `chat.allow` | Credential paths, the gateway's secrets, the harness state directory, `/proc`, `.env` files, `env`/`printenv`, `gh auth`, kubectl secrets/exec/port-forward/connection options, web fetch and search | An approval request |

The allow-list is not a sandbox. Claude Code itself lets a fixed set of read-only shell commands run in every mode, and applies path deny rules to them only on a best-effort basis. The approval gate is what stops a change.

## Install

Needs `uv`, the `gh` CLI signed in, `kubectl` if the agent should read a cluster, and a checkout of the site repository. The Claude Code CLI comes bundled with the Agent SDK.

1. **The Slack app.** Create it from [`slack-app-manifest.yaml`](slack-app-manifest.yaml) and install it to the workspace. Create an app-level token with `connections:write`. Upload [`../assets/icon-1024.png`](../assets/icon-1024.png) as the app icon (Basic Information > Display Information; a manifest cannot carry an icon). Invite the bot to the hub channel.
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
   # with a runner: the variables named by runner.user_env and runner.password_env
   # (a read-only Nagios login; removed from the environment before any agent starts)
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
| An unattended triage changes something | Triage-only rules deny record changes and pre-approved actions; every other call is refused without asking and logged | Claude Code's own read-only commands still run, as in every session |
| Alert text instructs the agent | The agent is told alert output is evidence; the triage-only policy bounds what any instruction could do | A misled triage can propose a bad fix; a person approves every change |
| An alert storm floods chat or spends the usage window | One triage per poll; alerts join an open incident; per-service cooldown | Each poll that finds new problems with no incident open can still start one triage |
| Unreviewed text on a public tracker | Triage records open in a private repository; only a responder's `promote` makes one public, carrying conclusions, not evidence | — |

## Development

```bash
cd plugins/sre-harness/gateway
uv sync --group dev
uv run pytest -q
uv run pylint src tests
uvx ruff check . && uvx ruff format --check .
```

Tests use an in-memory chat platform, agent and tracker (`tests/fakes.py`). A new chat platform implements `sre_gateway.chat.ChatAdapter`.
