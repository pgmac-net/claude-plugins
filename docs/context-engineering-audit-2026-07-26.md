# Claude Code docs cleanup — 2026-07-26

Ticket: [pgmac-net/homelabia#151](https://github.com/pgmac-net/homelabia/issues/151)

## Why

Anthropic's [context-engineering guidance for Claude 5-generation models](https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models) argues these models need *less* front-loaded context, not more — Anthropic removed 80%+ of Claude Code's own system prompt with no performance loss. Paul's prompt surface (global CLAUDE.md, auto-memory, 13 project CLAUDE.md files, and the `pgmac-workflows` plugin skills) had grown under the older "tell it everything" assumption and was costing tokens on every session while, in several places, holding rewritten-source-adjacent content that was directly discoverable from the tree — and in a few cases, quietly wrong.

## Approach

1. **Grilled the scope first** (`pgmac-workflows:grilling`) — settled seven decisions before touching anything: full scope (everything), gotchas-only CLAUDE.md style (<60L target), prune+tighten MEMORY.md, delete stale command duplicates, split/de-constrain the plugin skills, relocate displaced content to `docs/ARCHITECTURE.md`, one PR per repo with #151 as tracker.
2. **Rule for every file**: keep a line only if it's (a) a gotcha that would cause a wrong change, (b) a command not inferable from the project's own manifest, or (c) a constraint with no in-repo trace. Everything else moved to a docs file or was deleted.
3. Where a repo already had its own `docs/` (budgeteer, incidents, slack-scores), pointed there instead of creating a competing file.

## Always-on context (loads every session)

| Surface | Before | After |
|---|---|---|
| `MEMORY.md` (auto-memory index) | 105L / 1475w | 99L / 823w |
| `~/.claude/CLAUDE.md` | 15L / 210w | unchanged — already lean |

MEMORY.md: deleted 3 memory files for fully-resolved/orphaned items (an Opsera-removal note, a stale `mise`/`gh` fact, and a watch-cache delivery-gap bug fixed 2026-07-11 — its one still-useful "regression tell" signature was folded into `project_watch_cache_watchdog.md` instead of being lost). Every surviving index line tightened to a ≤12-word hook.

## `pgmac-workflows` plugin skills — [claude-plugins#6](https://github.com/pgmac-net/claude-plugins/pull/6)

- `create-pir/SKILL.md`: 282L → 44L router + `references/pir-workflow.md` for the full 9-step procedure (progressive disclosure)
- One rigid prohibition in `domain-modeling` softened to judgment-based phrasing. `pickup-ticket`'s plan-approval gate/model tiers and `grilling`'s one-question-at-a-time rule kept exactly as-is — those are load-bearing workflow constraints, not precautionary over-constraint
- Promoted the local `grill-with-docs`/`grill-me` launchers (previously loose files in `~/.claude/commands/`) into the plugin, versioned
- Added a new **`context-engineering` skill** — a durable checklist for writing or auditing any CLAUDE.md/AGENTS.md/skill, including post-processing `/init` output, so this doesn't need redoing by hand
- Bumped `pgmac-workflows` 1.2.0 → 1.3.0

Also deleted the two stale local duplicate skills (`domain-modeling`, `grilling`) from `~/.claude/commands/` — they'd drifted from the plugin's versions and this session's own skill listing showed both loading simultaneously. `grill-with-docs`/`grill-me` stay local until #6 merges (they only exist in the plugin on that branch so far).

## Project CLAUDE.md files — one PR per repo

| Repo | PR | Before | After | Notes |
|---|---|---|---|---|
| slack-scores | [#135](https://github.com/pgmac-net/slack-scores/pull/135) | 1063L | 29L | Marketing language, scorecards, and near-total duplication of 3180 lines already in docs/ARCHITECTURE.md/DEPLOYMENT.md/RUNBOOK.md/CONTRIBUTING.md. Fixed a stale claim about root-level docs that had actually moved to `docs/updates/`. |
| gh-issues-tui | [#92](https://github.com/pgmac-net/gh-issues-tui/pull/92) | 124L | 52L | Cut duplication with `docs/architecture.md`; kept the `tui/` state-machine invariants and design-invariant list verbatim (real regression traps, deliberately over the usual line target). Flagged `docs/architecture.md`'s own module tree as stale (predates an `app/`→`app/`+`event/`+`ui/` directory split) for a follow-up. |
| pgk8s | [#610](https://github.com/pgmac-net/pgk8s/pull/610) | 218L | ~15L | **Fixed a stale fact**: file listed Vault/Consul as deployed; both were removed months ago. |
| budgeteer | [#130](https://github.com/pgmac-net/budgeteer/pull/130) | 167L | ~40L | Pointed at existing, more detailed `docs/architecture.md` (382L) etc.; kept Database Migrations/Laravel Gotchas/Testing Gotchas verbatim — real MySQL/Laravel bugs, not restateable architecture. |
| ansible | [#226](https://github.com/pgmac-net/ansible/pull/226) | 90L | ~20L | **Fixed two wrong commands**: every example used `--vault-password-file` and `-i inventory/`, both contradicted by prior session feedback (vault password is env-var based; bare `-i inventory/` silently matches zero hosts). |
| pgmac.github.io | [#114](https://github.com/pgmac/pgmac.github.io/pull/114) | 90L | ~20L | |
| incidents | [#66](https://github.com/pgmac-net/incidents/pull/66) | 85L | 9L | Commands/Skills/Contributing sections were a near-exact duplicate of README.md. |
| docker-registry-walk | [#80](https://github.com/pgmac-net/docker-registry-walk/pull/80) | 75L | ~37L | Kept "Key design invariants" verbatim (password handling, bearer-token scoping bug, URL-join bug, Artifactory auth quirk) — genuine regression traps. |
| pgmac | [#16](https://github.com/pgmac/pgmac/pull/16) | 75L | ~20L | |
| terraform-pvek8s | [#15](https://github.com/pgmac-net/terraform-pvek8s/pull/15) | 62L | ~20L | |
| metasearch | [#27](https://github.com/pgmac-net/metasearch/pull/27) | 62L (never committed) | 17L | CLAUDE.md existed on disk but had never been committed — brought under version control already slim. |
| n8n-workflows | [#12](https://github.com/pgmac-net/n8n-workflows/pull/12) | 54L | 53L | Already close to the target — only the CI section restated the workflow YAML; everything else (API quirks, field whitelist) was already gotcha-shaped. |
| assessor | [#15](https://github.com/huh-sters/assessor/pull/15) | 46L | ~20L | **Dropped a stale Linear reference** — tickets migrated to GitHub Issues months ago. |

13 of 13 project files done, plus the plugin PR — 14 PRs total.

## What stayed long, deliberately

Three files (budgeteer, gh-issues-tui, slack-scores after the docs-duplication cut) still exceed the ~60-line rule of thumb. That's intentional: the content that survived in each is genuine, hard-won gotchas (MySQL/Laravel bugs, TUI state-machine invariants) that would bite silently if cut — the line-count target is a heuristic for "is this actually architecture in disguise," not a hard cap.

## What this didn't touch

- The GitHub Actions Slack workflow, dqlite/watch-cache/jiva runbooks, and other operational memory files — out of scope, not Claude Code prompt files
- Rewriting the stale `docs/architecture.md` module tree in gh-issues-tui (flagged in that PR, not fixed there)
- Any repo's actual code, tests, or CI config
