---
name: context-engineering
description: Use when writing or auditing a CLAUDE.md, AGENTS.md, or skill file — including right after running /init. Applies Anthropic's context-engineering rules for Claude 5-generation models so prompt files stay lean and don't regrow into knowledge repositories.
---

# Context Engineering

Claude 5-generation models need less front-loaded context, not more. Every line in an always-loaded file (CLAUDE.md, MEMORY.md) is a standing tax on every session; every line in a skill is a tax only when that skill loads. Spend the always-loaded budget on what genuinely can't be discovered any other way.

Source: [The new rules of context engineering](https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models).

## CLAUDE.md / AGENTS.md checklist

For every line, ask: **would Claude get this wrong by reading the repo instead?** If no, cut it.

Keep:
- 1–2 line repo purpose
- Commands not inferable from `package.json` / `pyproject.toml` / `Makefile` / `justfile`
- Gotchas: non-obvious constraints, footguns, "looks fine but silently breaks X"

Cut (move to `docs/ARCHITECTURE.md` or delete):
- Module-by-module or function-by-function descriptions
- File/directory listings
- API endpoint tables, database schemas — read the code instead
- Anything a `git log`/`git blame` or the test suite already shows

Target **under 60 lines**. If a rewrite still runs long, the content is probably architecture, not a gotcha — relocate it.

## Skill checklist

- **Hint, don't dictate.** Prefer "match the surrounding code's idiom" over exhaustive per-case examples — examples constrain exploration more than they guide it.
- **Split by progressive disclosure.** A SKILL.md over ~100 lines should become a short router (what/when/step list) plus `references/*.md` for full procedural detail. See `create-pir` in this plugin for the pattern.
- **Judgment over blanket prohibition**, except where a rule is genuinely load-bearing — a hard approval gate, a safety constraint, a sequencing requirement that breaks silently if skipped. Those stay explicit and rigid; soften everything else.
- **Reference over repetition.** Point at real code, tests, or an existing template rather than re-describing them in prose.

## Auditing existing files

1. `wc -l -w` the file — flag anything over the target.
2. Walk section by section against the "would Claude get this wrong by reading the repo" test.
3. For skills, check whether prohibitions are load-bearing (keep) or precautionary (soften to judgment-based phrasing).
4. Relocate displaced content before deleting it — don't just lose it.

## Post-processing `/init`

`/init`'s default output is a comprehensive knowledge dump — the exact shape this skill exists to trim. After running `/init`, run the CLAUDE.md checklist above on its output before accepting it.
