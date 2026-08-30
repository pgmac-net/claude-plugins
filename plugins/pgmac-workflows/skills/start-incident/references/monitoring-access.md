# Monitoring Access

Three sources, in priority order for Nagios data — each a fallback for the one above it — plus Slack, which is read regardless of whether Nagios is reachable.

## 1. Nagios MCP (preferred)

`https://nagios-mcp.int.pgmac.net/sse`, tools namespaced `mcp__nagios__*`.

**Configured at `/home/paul/pgmac` scope.** A session rooted at `/home/paul/pgmac/incidents` (or any other single-repo directory) will not have these tools available — go straight to the SSH fallback below rather than waiting on tools that don't exist in this session.

**The `-32602` gotcha — read this before concluding Nagios is down:**

Every `mcp__nagios__*` call failing with `MCP error -32602: Invalid request parameters` almost always means the client's SSE session reconnected without re-running `initialize` — not that Nagios or the MCP pod is unhealthy. This happens on any MCP pod restart or transport hiccup. **Fix: reconnect the MCP server (`/mcp`), don't fall through to the SSH fallback.** Falling through on a `-32602` wastes time solving a problem that isn't there and skips the tool that would've answered faster.

`get_overall_health_summary` can return `{"host_counts": null, "service_counts": null}` on an otherwise healthy session — that's a known quirk, not a signal. Use `get_alerts` for the real picture.

## 2. Direct — SSH + docker exec (fallback)

When the MCP is unavailable or out of scope for this session:

```bash
ssh macro 'docker exec nagios4 <command>'
```

- Nagios log: `/opt/nagios/var/nagios.log` inside the container — grep with `-a` (the log can contain byte sequences that make `grep` treat it as binary otherwise):
  ```bash
  ssh macro 'docker exec nagios4 grep -a "SERVICE ALERT" /opt/nagios/var/nagios.log | tail -20'
  ```
- Run a check manually from inside the container:
  ```bash
  ssh macro 'docker exec nagios4 check_nrpe -H <host> -c <check_name> -t 30'
  ```
- Nagios and the public status page both run as docker-compose services on `macro.int.pgmac.net`, not in the k8s cluster.

**If the incident is on `macro` itself**, this fallback may be unreachable too — the docs site (`macro.int.pgmac.net/incidents/...`) that Phase 3's runbook fallback would reach is on the same host. Don't be surprised when both fail together; fall to Slack.

## 3. Slack `#home-status` — always read, not just last resort

Read this channel regardless of whether Nagios is reachable, not only when it isn't. It carries signal Nagios never sees:

- Nagios alerts (mirrored here via webhook)
- Wazuh alerts at level 8+ (forwarded via the Wazuh integratord webhook — no Nagios equivalent)
- Remediation state transitions (self-healing scripts posting what they did)

Use the Slack MCP to read the channel; this skill does not post to it (see `sre-triage.md` — comms stay read-only here).

## Quick Decision

| Situation | Do |
|---|---|
| Session has `mcp__nagios__*` tools, calls succeed | Use them |
| Calls fail with `-32602` | Reconnect `/mcp`, retry — don't assume outage |
| No `mcp__nagios__*` tools in this session, or MCP genuinely down | `ssh macro 'docker exec nagios4 ...'` |
| Either of the above, in parallel | Also read `#home-status` for Wazuh/remediation signal Nagios won't show |
| The incident is on `macro` itself | Expect SSH and the runbook-site fallback to be dead together; lean on Slack and local runbook copies |
