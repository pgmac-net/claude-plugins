# claude-plugins

Claude Code plugin marketplace for pgmac-net workflows. These skills also work with [OpenCode](https://opencode.ai).

## Install for Claude Code

```
/plugin marketplace add pgmac-net/claude-plugins
/plugin install pgmac-workflows@pgmac
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
      ~/.config/opencode/skills/pickup-ticket \
      ~/.config/opencode/skills/grilling \
      ~/.config/opencode/skills/domain-modeling \
      ~/.config/opencode/skills/grill-with-docs \
      ~/.config/opencode/skills/grill-me \
      ~/.config/opencode/skills/context-engineering
```

### Forking

Fork this repo, add or modify skills under `plugins/<your-plugin>/skills/`, then update `scripts/install-opencode-skills.sh` and `scripts/install-opencode-skills.ps1` with your skill list. Users run the same install flow from your fork.

---

## Plugins

### pgmac-workflows

| Skill | Purpose |
|---|---|
| `create-pir` | Generate a post-incident review from the current session: Infinite How's root cause analysis, runbook evaluation, GitHub Issues for action items, commit + PR. Assumes the [incidents](https://github.com/pgmac-net/incidents) repo layout at `~/pgmac/incidents`. |
| `pickup-ticket` | Work a GitHub Issue end-to-end: read, grill, plan, post plan to the issue for approval, implement on a branch, raise a PR. Hard gate — no implementation before plan approval. |
| `grilling` | Interview relentlessly about a plan or design, one question at a time, until shared understanding is reached. |
| `domain-modeling` | Build and sharpen a project's `CONTEXT.md` glossary and ADRs as terminology and hard-to-reverse decisions crystallise. |
| `grill-with-docs` | `grilling` + `domain-modeling` together — a grill session that also writes the glossary/ADRs as it goes. |
| `grill-me` | `grilling` alone, invoked directly. |
| `context-engineering` | Audit or write a `CLAUDE.md`/`AGENTS.md`/skill against Anthropic's context-engineering rules for Claude 5-generation models — gotchas-only, progressive disclosure, judgment over blanket prohibition. |

`create-pir` and `pickup-ticket` default to `pgmac-net/homelabia` when no owning repo is identifiable. Adapt paths and defaults if you're not me.

## Adding a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json` and `skills/`, `agents/`, or `commands/` directories
2. Add the plugin entry to `.claude-plugin/marketplace.json`
3. Bump versions in both files
4. PR

## Development loop

Clone this repo and add it as a local marketplace for instant edits:

```
/plugin marketplace add ~/pgmac/claude-plugins
```

Other machines consume it from GitHub and pick up changes on marketplace update.
