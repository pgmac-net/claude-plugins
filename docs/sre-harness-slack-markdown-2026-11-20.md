# sre-harness 0.21.1: Slack replies render Markdown

Ticket: pgmac-net/homelabia#280 (closed 2026-11-21) · PRs: claude-plugins#58 (0.21.0) and #59 (0.21.1) · Deploy pins: ansible#371 and #372 · Site config: sre-catalog#69

## Problem

The ticket asked what is needed for the gateway's Slack messages to use Markdown, so incident updates are easier to read. The gateway already converted bold, headings and tables, but the result was escaped plain `text`: links never worked, italic showed as bold, and strikethrough and quotes did not render.

## What shipped

**0.21.0**

- Agent replies go out as a Slack `markdown` block (`to_markdown_block` in `slack.py`). The message `text` keeps the old escaped conversion as the notification text and as the fallback.
- A reply over 12,000 characters, or a block Slack refuses, is sent as text; the first refusal for each reason is logged. Edits send `blocks: []` on fallback so stale blocks do not stay.
- Mentions cannot form: `<@`, `<!`, `<#` and `<scheme:` get a zero-width space after the `<`, in prose and in code. Code is parsed per CommonMark (fences, equal-length backtick runs, backslash escapes); the extra step in code covers Slack reading it differently. Ordinary code such as `<<EOF` is untouched.
- New `chat.link_hosts` in `site.yaml` (schema, README; `validate_site.py` checks it through the schema). A Markdown link is clickable only for https URLs on a listed host (`*.suffix` for subdomains); other links show as `label (`url`)`; bare off-list `https://` and `www.` addresses go in inline code; images and reference-style links are defused. Empty by default, so nothing is clickable until a site lists hosts.
- ADR 0024, README "Message formatting" section, CONTEXT.md term "Link allowlist". Plugin 0.21.0, gateway 0.16.0.

**0.21.1, found on the first deployment**

- Tables and emoji rendered, but no link was clickable. The agent wrote URLs bare or inside backticks, and neither is a link in a markdown block; the converter was passing listed `[label](url)` links through correctly.
- An https address on a listed host becomes `[address](address)` when it is bare or the whole content of a single-backtick span. A command containing a URL stays code. `http://` addresses are never linked, so an internal address such as Nagios on `:8080` stays unclickable.
- The prompt tells the agent to write `**bold**` (one asterisk is italic) and links as `[label](https://host/path)` outside backticks.
- Plugin 0.21.1, gateway 0.16.1.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Native block or a better converter? | Native `markdown` block | Real tables, quotes, strikethrough and links without maintaining a converter |
| Links | Allowlisted hosts in `site.yaml` | Agent text repeats alert and log content; an unrestricted link is a phishing path |
| Mentions | Always neutralised, in code too | Slack's behaviour inside the block is undocumented, so the design must not depend on it |
| Messages over 12,000 characters | Fall back to text | Splitting was left out of scope |
| Bare URLs inside code | Left alone | Shown as written; an invisible character would break copy-paste |

## Review findings

An automated security review of the first commit found that the code detection (a split on single backticks, three-backtick fences only) could disagree with Markdown and let a mention through. It was fixed with CommonMark parsing plus the extra step in code. A second review round fixed backslash parity (`\\` followed by a backtick).

## Deviations from the plan

- The Slack spike (mention parsing, `chat.update` with blocks, nested lists) was not run: no Slack workspace was available to the build. The design did not depend on it, and the deployment check covered what mattered: tables, emoji and edits rendered, and links needed the 0.21.1 fix.
- The ansible pins and the site `chat.link_hosts` list went in separate PRs, as planned. sre-catalog's CI pinned an older harness commit that rejected `link_hosts`; the pin was bumped in sre-catalog#69.

## Verification

Gateway: 238 tests, ruff, ruff format and pylint (exit 0). Plugin: 806 tests. After each deploy, in #incidents: tables, emoji and, after 0.21.1, links on listed hosts were confirmed by the owner.

## Deploying (for the next change)

1. Merge the claude-plugins PR, then pin `sre_gateway_plugin_version` in ansible to the merge commit.
2. Run `sre-gateway.yml`; the gateway restarts as a user service on macro.
3. The site repository's CI pins a harness commit (`SRE_HARNESS_REF` in three workflows); bump it when `site.yaml` uses a new key.

Notion: https://app.notion.com/p/41e524b4a07781fb8244f16048f0b4ed
