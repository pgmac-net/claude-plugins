# claude-plugins

Claude Code plugin marketplace for pgmac-net workflows. These skills also work with [OpenCode](https://opencode.ai) and [Pi](https://pi.dev).

## Install for Claude Code

```
/plugin marketplace add pgmac-net/claude-plugins
/plugin install pgmac-workflows@pgmac
/plugin install sre-harness@pgmac
```

## Install for OpenCode

Install the skills globally so they're available in any project.

### Quick install

**Linux / macOS:**

1. Clone the repo:
   ```bash
   git clone git@github.com:pgmac-net/claude-plugins.git ~/pgmac/claude-plugins
   ```

2. Run the install script:
   ```bash
   scripts/install-opencode-skills.sh
   ```

**Windows (PowerShell as Administrator or with [Developer Mode](https://learn.microsoft.com/en-us/windows/apps/get-started/enable-your-device-for-development) enabled):**
```powershell
.\scripts\install-opencode-skills.ps1
```

### Manual install

1. Clone the repo:
   ```bash
   git clone git@github.com:pgmac-net/claude-plugins.git ~/pgmac/claude-plugins
   ```

2. Create the global skills directory:
   ```bash
   mkdir -p ~/.config/opencode/skills
   ```

3. Symlink each skill:
   ```bash
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/create-pir           ~/.config/opencode/skills/create-pir
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/start-incident       ~/.config/opencode/skills/start-incident
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/pickup-ticket        ~/.config/opencode/skills/pickup-ticket
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/grilling             ~/.config/opencode/skills/grilling
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/domain-modeling      ~/.config/opencode/skills/domain-modeling
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/grill-with-docs      ~/.config/opencode/skills/grill-with-docs
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/grill-me             ~/.config/opencode/skills/grill-me
   ln -sfn ~/pgmac/claude-plugins/plugins/pgmac-workflows/skills/context-engineering  ~/.config/opencode/skills/context-engineering
   ```

4. Restart opencode.

### Uninstall

**Script:**
```bash
scripts/install-opencode-skills.sh --uninstall
```
or
```powershell
.\scripts\install-opencode-skills.ps1 -Uninstall
```

**Manual:**
```bash
rm -rf ~/.config/opencode/skills/create-pir \
      ~/.config/opencode/skills/start-incident \
      ~/.config/opencode/skills/pickup-ticket \
      ~/.config/opencode/skills/grilling \
      ~/.config/opencode/skills/domain-modeling \
      ~/.config/opencode/skills/grill-with-docs \
      ~/.config/opencode/skills/grill-me \
      ~/.config/opencode/skills/context-engineering
```

### Forking

Fork this repo, add or modify skills under `plugins/<your-plugin>/skills/`, then update `scripts/install-opencode-skills.sh` and `scripts/install-opencode-skills.ps1` with your skill list. Users run the same install flow from your fork.

## Install for Pi

Pi implements the [Agent Skills spec](https://agentskills.io/specification), so the skills load unchanged. Pick one install method — loading both makes Pi warn about duplicate skill names and keep the first copy it finds.

### Package install

```bash
pi install git:github.com/pgmac-net/claude-plugins
```

Append `@vX.Y.Z` to pin a release. Update an unpinned install with `pi update git:github.com/pgmac-net/claude-plugins`. The root `package.json` maps every `plugins/*/skills` directory into the package.

### Local clone

For development, point Pi at the clone so edits take effect on `/reload`. Add to `~/.pi/agent/settings.json`:

```json
{
  "skills": ["~/pgmac/claude-plugins/plugins/pgmac-workflows/skills"]
}
```

### Using the skills in Pi

Skills trigger from their descriptions, or explicitly with `/skill:<name> [args]`:

```
/skill:pickup-ticket pgmac-net/homelabia#42
```

- `pickup-ticket` asks you to switch models using [pi-claude-bridge](https://github.com/elidickinson/pi-claude-bridge) IDs such as `/model claude-bridge/claude-sonnet-5-5`. Install the bridge with `pi install npm:pi-claude-bridge`, or decline the switch and the skill carries on with the current model.
- Steps that use MCP servers need those servers added to Pi with `pi mcp add`: the Notion page in `pickup-ticket` and the Nagios tools in `start-incident`. Pi doesn't support SSE transport, which the Nagios MCP endpoint uses, so `start-incident` falls back to SSH.

---

## Plugins

### pgmac-workflows

| Skill | Purpose |
|---|---|
| `create-pir` | Generate a post-incident review from the current session: Infinite How's root cause analysis, runbook evaluation, GitHub Issues for action items, commit + PR. Assumes the [incidents](https://github.com/pgmac-net/incidents) repo layout at `~/pgmac/incidents`. Reads a `start-incident` tracking issue as its primary source when one exists. Embeds `sargraph` SVG graphs of SAR data and nagiosgraph PNGs of the alerting Nagios checks where they show root cause or recovery. |
| `start-incident` | Work a live homelab incident as a Senior/Staff SRE: open a tracking issue immediately, triage via Nagios/Slack, match a runbook, gate every mutating action on confirmation, graph SAR data to SVG with [`sargraph`](https://github.com/pgmac-net/sargraph) when resource exhaustion is in play, embed signed nagiosgraph graphs of alerting checks (the same mechanism as `notify_slack.sh`), and leave a structured timeline that `create-pir` consumes directly once resolved. |
| `pickup-ticket` | Work a GitHub Issue end-to-end: read, grill, plan, post plan to the issue for approval, implement on a branch, raise a PR, and post a completion summary back to the issue. Hard gate — no implementation before plan approval. Tracks progress with `in-progress` / `blocked` / `in-review` state labels, creating them if the repo lacks them. |
| `grilling` | Interview relentlessly about a plan or design, one question at a time, until shared understanding is reached. |
| `domain-modeling` | Build and sharpen a project's `CONTEXT.md` glossary and ADRs as terminology and hard-to-reverse decisions crystallise. |
| `grill-with-docs` | `grilling` + `domain-modeling` together — a grill session that also writes the glossary/ADRs as it goes. |
| `grill-me` | `grilling` alone, invoked directly. |
| `context-engineering` | Audit or write a `CLAUDE.md`/`AGENTS.md`/skill against Anthropic's context-engineering rules for Claude 5-generation models — gotchas-only, progressive disclosure, judgment over blanket prohibition. |

`create-pir` and `pickup-ticket` default to `pgmac-net/homelabia` when no owning repo is identifiable. Adapt paths and defaults if you're not me.

### sre-harness

A portable SRE harness: the plugin holds logic only, and everything about one site lives in a private site repository you own. This release is the foundation; incident, catalog and SLO skills follow. See [`plugins/sre-harness/README.md`](plugins/sre-harness/README.md).

| Skill | Purpose |
|---|---|
| `site-check` | Find the site repository, validate `site.yaml` and the tier definitions, and report what is configured and what is missing. |

The scripts need [`uv`](https://docs.astral.sh/uv/). The OpenCode install scripts cover `pgmac-workflows` only; for `sre-harness`, symlink `plugins/sre-harness/skills/site-check` into `~/.config/opencode/skills/` by hand. Pi picks it up through the `package.json` glob.

## Adding a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json` and `skills/`, `agents/`, or `commands/` directories
2. Add the plugin entry to `.claude-plugin/marketplace.json`
3. Bump versions in both files
4. PR

Pi loads `plugins/<name>/skills/` through the `package.json` glob, so a new plugin's skills need no manifest change. `agents/` and `commands/` are Claude Code only.

## Development loop

Clone this repo and add it as a local marketplace for instant edits:

```
/plugin marketplace add ~/pgmac/claude-plugins
```

Other machines consume it from GitHub and pick up changes on marketplace update.
