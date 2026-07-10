# claude-plugins

Claude Code plugin marketplace for pgmac-net workflows.

## Install

```
/plugin marketplace add pgmac-net/claude-plugins
/plugin install pgmac-workflows@pgmac
```

## Plugins

### pgmac-workflows

| Skill | Purpose |
|---|---|
| `create-pir` | Generate a post-incident review from the current session: Infinite How's root cause analysis, runbook evaluation, GitHub Issues for action items, commit + PR. Assumes the [incidents](https://github.com/pgmac-net/incidents) repo layout at `~/pgmac/incidents`. |
| `pickup-ticket` | Work a GitHub Issue end-to-end: read, plan, post plan to the issue for approval, implement on a branch, raise a PR. Hard gate — no implementation before plan approval. |

Both skills default to `pgmac-net/homelabia` when no owning repo is identifiable. Adapt paths and defaults if you're not me.

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
