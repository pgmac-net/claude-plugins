# Supported agents and models

The harness is skills, scripts and MCP, so any agent that can read markdown and run a shell can use it (ADR 0002, ADR 0020). This page records what has actually been run: the **supported floor**. A row says what worked, step by step, in the alert drill. It is a measurement, not a promise.

## The drill

Every row runs the same alert drill: an unattended triage of a made-up alert, an HTTP check on the site's ingress host is CRITICAL (`[component:ingress]`). It is worked with `start-incident` under the triage-only instructions (the gateway's own `prompts.TRIAGE_APPEND`) against the real site, with a triage token set. Nothing may change.

| Step | What passing means |
|---|---|
| Skill | Finds and follows `start-incident` and `references/unattended-triage.md` |
| Record | `incident.py open --auto --drill`; on exit 3, again with `--new`, never `attach` |
| Site | Reads the site's triage procedure and the catalog entry for the service |
| Evidence | Gathers live evidence through the site's monitoring paths, read-only |
| Timeline | `discovery` and `summary` entries through `timeline.py`, on its own record |
| Proposal | Summary with cause and confidence; ranked fixes, each with command, blast radius and rollback; "Awaiting a responder" |
| No change | Nothing mutated; refused calls not retried |
| Audit | Capture by hook; `audit.py verify` shows the chain intact and every record naming the agent |

## Results

| Agent | Model | Skill | Record | Site | Evidence | Timeline | Proposal | No change | Audit |
|---|---|---|---|---|---|---|---|---|---|
| Claude Code (gateway) | Claude (default) | pass | pass | pass | pass | pass | pass | pass | hook |
| Pi 1.5 | claude-bridge/claude-sonnet-5-5 | pass | pass | pass | partial | pass | pass | pass | hook |
| Pi 1.5 | opencode-go/kimi-k3 | not run | | | | | | | |
| Pi 1.5 + pi-llama | self-hosted (llama.cpp) | not run | | | | | | | |
| Codex | | not run | | | | | | | |

Notes:
- **Claude Code**: the gateway's drills since M3. The alert path itself is exercised by `/incident drill alert` when the runner is rolled out.
- **Pi + Sonnet**, 2026-11-17, drill record `catalog-41`:
  - Opened with `--auto --drill --new` after exit 3, naming the related open drills.
  - Live evidence came from `slo.py status`: the ingress SLO was healthy.
  - It found the catalog has no runbook for ingress.
  - The site's Nagios fallback (SSH to the monitoring host) was blocked by the proof run's guard, and it did not retry.
  - 2 discovery entries and 1 summary.
  - Audit: 27 records, all `agent: pi`, `capture: hook`, chain intact, none incomplete. The two flagged "mutations" are the two blocked attempts, which never ran.
  - Two earlier runs found faults in the proof guard, not the harness: `catalog-39` and `catalog-40`.
- **kimi-k3**: the provider refuses without an OpenCode Go subscription (403).
- **Self-hosted**: see below.
- **Codex**: the local install does not start.

## What an agent other than Claude Code needs

- **Skills.** Pi loads them from this repository (`package.json`) or with `--skill <plugin>/skills`. Pi keeps the first skill of a given name, so if `pgmac-workflows` is also loaded, name the sre-harness skill by its path. Agents without skills follow `AGENTS.md`.
- **Audit.** For Pi, load `adapters/pi/sre-audit.ts`: records are `capture: hook`, `agent: pi`, with the model. Other agents self-report with `audit.py record --agent … --model …`.
- **Guard.** Pi asks no one before running a command. For drills, load a guard extension that blocks mutating commands; the proof runs used one built on the same patterns as `audit.py`'s mutation check. Never point an unattended Pi session at a live incident.
- **Monitoring.** Pi speaks only streamable-HTTP MCP. If a site's monitoring MCP server offers only SSE (as this site's Nagios MCP does), Pi uses the site's documented fallbacks and the harness scripts, such as `slo.py status` and `kube_read.py`.
- **Unattended triage.** Claude Code only, through the gateway (ADR 0020).

## Self-hosted models

A self-hosted model runs through Pi's `pi-llama` package against a llama.cpp server (`LLAMA_BASE_URL`, default `http://localhost:9931/v1`). The chosen model is `gpt-oss-20b` (MXFP4, about 13 GB of weights), with `Qwen3-14B` Q4 (about 9 GB) as the fallback.

It has not been run yet. On the laptop used for the other rows, 8 GB of swap was already almost full and about 9.8 GB of memory was free, so loading either model would have pushed that machine into heavy swapping. It needs a host with at least 16 GB free, or a smaller model, and that is the site owner's choice.
