# sre-harness 0.21.0: Slack replies render Markdown

Ticket: pgmac-net/homelabia#280 · Branch: `280-slack-markdown-block`

## Problem

The ticket asked what is needed for the gateway's Slack messages to use Markdown, so incident updates are easier to read. The gateway already converted bold, headings and tables, but the result was escaped plain `text`: links never worked, italic showed as bold, and strikethrough and quotes did not render.

## What changed

- Agent replies go out as a Slack `markdown` block (`to_markdown_block` in `slack.py`). The message `text` keeps the old escaped conversion as the notification text and as the fallback.
- A reply over 12,000 characters, or a block Slack refuses, is sent as text. Edits send `blocks: []` on fallback so stale blocks do not stay.
- Mentions cannot form: `<@`, `<!`, `<#` and `<scheme:` get a zero-width space after the `<`, in prose and in code (code is parsed per CommonMark; the extra step in code covers Slack reading it differently).
- New `chat.link_hosts` in `site.yaml` (schema, README, checked by `validate_site.py` through the schema). A Markdown link is clickable only for https URLs on a listed host (`*.suffix` for subdomains); other links show as `label (`url`)`; bare off-list `https://` and `www.` addresses go in inline code; images and reference-style links are defused. Empty by default, so nothing is clickable until a site lists hosts.
- The system prompt now tells the agent that tables, headings and quotes render, and that links are limited.
- ADR 0024, README "Message formatting" section, CONTEXT.md term "Link allowlist". Plugin 0.21.0, gateway 0.16.0.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Native block or a better converter? | Native `markdown` block | Real tables, quotes, strikethrough and links without maintaining a converter |
| Links | Allowlisted hosts in `site.yaml` | Agent text repeats alert and log content; an unrestricted link is a phishing path |
| Mentions | Always neutralised | Slack's behaviour inside the block is undocumented, so the design must not depend on it |
| Messages over 12,000 characters | Fall back to text | Splitting was left out of scope |

## Deviations from the plan

- The Slack spike (mention parsing, `chat.update` with blocks, nested lists) was not run: the build had no Slack workspace to test in. The design does not depend on it (see ADR 0024). The first deployment should check an edit, a table and a listed link.
- The plan's follow-up PRs (ansible version pin, `chat.link_hosts` in the site repository) are not part of this PR.

## Verification

Gateway: 218 tests, ruff and pylint (exit 0). Plugin: 806 tests, ruff check and format.

## Deploying

1. Merge this PR, then pin `sre_gateway_plugin_version` in the ansible repository to the merge commit.
2. Add `chat.link_hosts` to the site repository's `site.yaml` before or with the deploy, or links stay inert.
3. After deploy, check in Slack: a table, an edit (the working indicator), and a link to a listed host.
