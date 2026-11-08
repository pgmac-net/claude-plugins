# Nagios Graphs (nagiosgraph)

Every Nagios check that emits perfdata has an RRD history in nagiosgraph. A graph of the check that alerted, drawn with its warning and critical thresholds, shows when the metric crossed the line and how far it went. That is direct evidence for the timeline and the PIR.

Use it alongside `sar-graphs.md`, not instead of it:

| nagiosgraph | sargraph |
|---|---|
| The exact check that alerted, with its thresholds | Host-level resource detail (per-core, per-disk, per-interface) |
| Any monitored host, including k8s nodes with sysstat disabled | Only hosts with sysstat enabled and recording |
| History up to a year (RRD), so slow trends and "since when?" | Days to weeks, until the `saDD` files rotate |
| Checks with no SAR equivalent: kine reconnects, dqlite WAL, pending pods, cert days left | Fine-grained 1–10 min samples, picked per metric |
| Fixed nagiosgraph default view per service, PNG only | Any metric combination, SVG |

## How It Works

The graph comes from the same mechanism `notify_slack.sh` ([pgmac-net/nagios-config](https://github.com/pgmac-net/nagios-config)) uses to embed graphs in Slack alerts. The internal Nagios sits behind Cloudflare Access, so the public status page proxies nagiosgraph at `https://statuspage.pgmac.net.au/api/graph`. Each request must carry an HMAC-SHA256 signature over `host|service|period|timet|expires`. The proxy contract is documented in `docs/SLACK_GRAPH_PROXY.md` in [pgmac-net/nagios-public-status-page](https://github.com/pgmac-net/nagios-public-status-page).

`scripts/nagios-graph.sh` (in this skill's directory) builds the URL. It reads the signing secret from `slack.ini` inside the `nagios4` container over `ssh macro` and never prints it:

```bash
# <skill-base-dir> is the "Base directory for this skill" shown when the skill loaded.
# From start-incident:  <skill-base-dir>/scripts/nagios-graph.sh
# From create-pir:      <skill-base-dir>/../start-incident/scripts/nagios-graph.sh
NG=<skill-base-dir>/scripts/nagios-graph.sh
$NG -h    # full usage
```

The plugin cache path is versioned, so don't hardcode it. It needs `ssh macro`, `openssl`, `jq`, and `curl` locally.

- `-p` sets the period: `day` (~32h), `week` (~9 days), `month`, `quarter`, or `year`.
- `-t` sets the window **end** (the anchor). It takes an epoch, anything `date -d` parses (`'2026-11-08 15:30'`, `'2 hours ago'`, in local AEST), or `live`. The default is now.
- `-o file.png` downloads the graph and validates it. The exit code is `2` when the graph is empty.
- `-T` sets the signature lifetime in seconds, from 60 up to 30 days. The default is the maximum, 30 days.

**Always anchor.** With `-t live` (timet=0) the URL redraws "now" on every fetch, so a timeline comment read next week would show next week's graph. The default anchor (now) freezes the window to what the responder saw.

**Validate a 200 response.** An unknown host or service, or a check with no RRD data, still returns HTTP 200. The body is a 608×23 "No data available" PNG. `-o` checks the image height and fails with exit 2, so always download through the script rather than trusting a bare URL.

**Exposure.** `pgmac-net/incidents` is a public repo. An embedded signed URL lets anyone fetch that one anchored graph until it expires, and a committed PNG is public permanently. This matches the SAR SVGs already committed there. Don't graph a check whose output itself is sensitive.

**If `macro` is down**, Nagios and the graph proxy go with it. Skip nagiosgraph and lean on SAR (`sar-graphs.md`).

## 1. Pick the Services

Graph only checks with perfdata. `-L` lists them for a host:

```bash
$NG -L k8s03
```

Choose in this order, and keep it to 1–4 graphs per host:

1. **The checks that alerted** during the incident. Use the `SERVICE ALERT` lines from the Nagios MCP `get_alerts`, `#home-status`, or the Nagios log (`monitoring-access.md`).
2. **The resource checks on affected hosts** that match the suspected cause: `Load Average`, `CPU Stats`, `ram`, `Check swap`, `Root partition`, and the k8s `microk8s-*` checks (`microk8s-dqlite-wal`, `microk8s-kine-reconnect-failures`, `microk8s-pending-pods`, `microk8s-watch-cache`).
3. **The same check on an unaffected peer** (k8s01 vs k8s03), when you need to show the problem was host-specific.

Service names are case- and space-exact (`Load Average`, not `load`). Copy them from `-L`.

## 2. Pick the Window

The anchor is the window's end, and the period sets how far back it reaches.

| Purpose | Period | Anchor (`-t`) |
|---|---|---|
| Live incident: what the check shows right now | `day` | default (now) |
| Onset: when it started, with a baseline | `day` | about 1h after onset |
| Slow leak, creeping disk, "since when?" | `week` or `month` | now or recovery |
| PIR recovery: baseline → onset → recovery | `day` (or `week` if the incident ran over a day) | 1–2h after recovery |

## 3. Get the Graph

During the incident, save every graph into the evidence directory (`sar-graphs.md` §1 sets `$EV`):

```bash
URL=$($NG -p day -o "$EV"/k8s03-load-average.png k8s03 'Load Average')
```

Look at the PNG yourself with the Read tool before you describe it. The image carries no host or service title, so the filename, alt text, and caption have to name both.

## 4. Record It

A GitHub issue comment can embed the signed URL as an image. GitHub proxies it, and the 30-day signature outlives the incident long enough for `create-pir` to collect it:

```markdown
**14:32 AEST** — nagiosgraph k8s03 `Load Average` (day, to 14:30): 1-min load 0.3 → 9.8 from 13:55, past critical (10) by 14:20. Local: ~/incident-evidence/incidents-87/k8s03-load-average.png

![k8s03 Load Average, day to 14:30 AEST: load climbs from 13:55, crosses critical 14:20](<signed URL>)
```

After recovery, regraph the same services anchored after the fix so the return to baseline is on record. `create-pir` downloads the PNGs into the incidents repo, because the signed URLs expire.
