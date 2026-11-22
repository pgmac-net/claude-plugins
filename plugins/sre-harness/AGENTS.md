# sre-harness, for agents other than Claude Code

This directory is an SRE harness: skills (markdown) and scripts (Python, run with `uv`). They work an incident, keep its record and audit log, maintain a service catalog and report on SLOs. Claude Code loads it as a plugin. Any other agent can use it by following this file.

Which agents and models are known to work, and how well: `docs/supported-agents.md`.

## Where things are

- **The plugin root** is this directory. When a skill says `<plugin root>`, it means this directory's absolute path. Run scripts as `uv run <plugin root>/scripts/<script>.py …`, by full path, one command at a time.
- **`srectl` is for people, not for you.** It is a terminal front end over these scripts. Never run it; the chat policy and the audit log recognise only the full-path form above.
- **Skills** are in `skills/<name>/SKILL.md`. If your agent loads skills, point it at `skills/`. If it doesn't, read the matching `SKILL.md` yourself when the task fits its description (the `description:` line at the top), and follow it as written. Paths in a skill are relative to that skill's directory.
- **The site** is a separate private repository with `site.yaml`. Scripts find it through `SRE_HARNESS_SITE` (its path) or `~/.config/sre-harness/site`. If neither is set, stop and ask; never guess a site.
- **Terms** are defined in `CONTEXT.md`. Use them as defined.

## Rules that hold whatever the agent

1. **Authority tiers** (`docs/adr/0006-tiered-autonomy.md`):
   - Tier 0, read-only: run freely.
   - Tier 1, any change to a system: state the blast radius, wait for a person's clear yes, record it, then act.
   - Tier 2: decided only by `scripts/autonomy.py match`, never by you.

   A drill changes nothing.
2. **Open the record first.** `scripts/incident.py open` before diagnosis. Every timeline entry goes through `scripts/timeline.py`; never post one by hand.
3. **The audit log** (`skills/start-incident/references/audit-log.md`):
   - If your agent runs Claude Code hooks, or is Pi with `adapters/pi/sre-audit.ts` loaded, capture is automatic.
   - Otherwise you record each command yourself, right after running it:

     ```bash
     uv run <plugin root>/scripts/audit.py record --kind command --command "<exact command>" \
       --text "<what happened>" --agent <your agent name> --model <provider/model>
     ```

     Self-reported records are weaker, and the review says so. Record approvals with `--kind approval` before acting.
4. **Never read or print a secret.** Never touch `~/.local/state/sre-harness/` except through the scripts.
5. **Say what you don't know.** "Not known yet" beats a guessed root cause.

## Pi

Pi loads these skills through this repository's `package.json`, or through `skills` in `~/.pi/agent/settings.json`. Turn on audit capture by adding the adapter to `~/.pi/agent/settings.json`:

```json
{ "extensions": ["<plugin root>/adapters/pi/sre-audit.ts"] }
```

It sends each tool call to `scripts/audit_hook.py` as a Claude Code hook would, with `agent: pi` and the model, and never blocks a call. It records nothing outside an incident.

For monitoring access, configure the site's MCP servers in Pi's MCP configuration, read-only tools only.

The chat gateway can also run Pi itself, with the chat policy and approvals applied through `adapters/pi/sre-policy.ts`: see "Running on Pi" in `gateway/README.md` (ADR 0021). That extension only works under the gateway; outside it, it blocks every call.
