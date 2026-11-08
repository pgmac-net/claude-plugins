# Nagios graphs in start-incident and create-pir

**Ticket:** [pgmac-net/claude-plugins#21](https://github.com/pgmac-net/claude-plugins/issues/21)
**Branch:** `21-nagios-graphs`
**Notion:** [claude-plugins#21: Nagios graphs in start-incident and create-pir](https://app.notion.com/p/412524b4a07781acbe02cc67a48a7c50)
**Follows:** [#19 sargraph SAR graphs](sargraph-incident-graphs-2026-11-08.md)

## What changed

`start-incident` and `create-pir` now add nagiosgraph graphs of the Nagios checks involved in an incident, next to the `sargraph` SAR graphs. A nagiosgraph graph shows the exact check that alerted, drawn with its warning and critical thresholds, from up to a year of RRD history. That covers hosts with sysstat disabled (the k8s nodes) and SAR windows that have already rotated out.

### Mechanism

The skills use the same mechanism as `notify_slack.sh` in [pgmac-net/nagios-config](https://github.com/pgmac-net/nagios-config): an HMAC-SHA256-signed, expiring URL to the public status page's `/api/graph` proxy (`https://statuspage.pgmac.net.au`, contract in `docs/SLACK_GRAPH_PROXY.md` in [pgmac-net/nagios-public-status-page](https://github.com/pgmac-net/nagios-public-status-page)).

```
sig = HMAC-SHA256(GRAPH_SIGNING_SECRET, "host|service|period|timet|expires")
```

- `period` is one of `day` (~32h), `week` (~9 days), `month`, `quarter`, `year`.
- `timet` is the absolute end of the window. `0` means live, and a live graph changes every time the URL is fetched, so the skills never use it in a record.

### `scripts/nagios-graph.sh` (new, in start-incident)

```
nagios-graph.sh [-p period] [-t when] [-T ttl] [-o out.png] <host> <service>
nagios-graph.sh -L <host>     # list graphable services (have perfdata)
```

- Reads `GRAPH_SIGNING_SECRET` and `STATUSPAGE_BASE_URL` from `slack.ini` inside the `nagios4` container over `ssh macro`. The secret is never printed. Environment variables can override both.
- `-t` takes an epoch, any `date -d` string, or `live`. The default is now, so the window stays fixed at what the responder saw.
- The default TTL is 30 days, long enough for the issue-comment image to last until the PIR is written.
- `-o` downloads the PNG and validates it.
- Exit codes: `0` ok, `1` usage/config, `2` no graph data, `3` fetch failed.

### `start-incident` 1.1.0 → 1.2.0

- New `references/nagios-graphs.md`, shared with `create-pir`. It covers when to choose nagiosgraph or sargraph, choosing services (alerting checks first, then resource checks, then an unaffected peer), choosing the window, validating, exposure, and recording.
- Phase 2 notes which checks alerted on which hosts.
- Phase 4 graphs alerting checks, and hosts without SAR data, and embeds the signed URL in the timeline comment with a local PNG copy.
- Phase 5 regraphs anchored after recovery.
- nagiosgraph is added to the read-only diagnostics allowed without confirmation.

### `create-pir` 1.7.0 → 1.8.0

- Step 1 collects the Nagios graphs, or makes them for alerting checks in a chain.
- Step 4 downloads fresh PNGs anchored 1–2h after recovery into `src/incidents/img/<pir-stem>/`. The PIR never embeds a signed URL, because the URL expires. Captions must name the host, the service, and the threshold, because the PNG has no title.
- The commit step, PR body, and checklist are updated.

Plugin `pgmac-workflows` 1.10.0 → 1.11.0.

## Findings from testing

- **A 200 response can still be empty.** An unknown host or service, or one with no RRD data, returns HTTP 200 with a 608×23 "No data available" PNG. `notify_slack.sh` only checks for 200, so it would embed that placeholder. `nagios-graph.sh` checks the PNG height (from the IHDR) and rejects anything under 50px.
- The anchor works: a `week` graph anchored 24h ago ends the previous day.
- Service names containing spaces (`Load Average`) sign and fetch correctly once they are URL-encoded.
- A tampered signature returns 400.
- **Exposure:** `pgmac-net/incidents` is public. An embedded signed URL exposes one fixed-window graph until it expires, and a committed PNG is public permanently. The SAR SVGs already committed there are public in the same way.

## Follow-ups

- `notify_slack.sh` / `notify_zulip.sh` could apply the same height check, so a "No data available" placeholder never reaches Slack.
