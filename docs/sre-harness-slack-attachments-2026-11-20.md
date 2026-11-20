# sre-harness 0.20.0: Slack snippets and text files reach the gateway agent

Ticket: pgmac-net/homelabia#270 · Branch: `270-slack-text-attachments`

## Problem

A responder who sent a long log excerpt as a Slack snippet, or attached a text file, got no reaction. The gateway's Slack handler (`slack.py`) dropped every `message` event that had a `subtype`, and a snippet, an upload, or a long paste that Slack converts to a snippet arrives as `subtype: file_share`. The typed text on the same message was dropped with it, and nobody was told.

The ticket asked three things: can it be done, what does it need, and what are the pros and cons.

## What changed

- `slack.py` lets `file_share` through (every other subtype is still ignored) and reads each file's metadata into a platform-neutral `FileRef`. A new `download()` fetches the file with the bot token, only from `*.slack.com`, without following redirects, stopping at the size cap.
- New `attachments.py` does the work: text detection, bounded reading (head and tail), redaction in pieces within `redact_text`'s scan length, the fenced "untrusted data" block for the agent, a one-line description for the issue, and the notice for files that were skipped.
- `gateway.py` calls it wherever a responder's message reaches the agent (hub mention start, incident channel, question thread, triage join). The issue mirror carries the description and the permalink, never the content.
- `chat.attachments` in `site.yaml` configures the caps; `site.schema.json` and `validate_site.py` check them.
- `files:read` added to the Slack app manifest; ADR 0023; glossary term **Attachment**; gateway README section; one sentence in both agent prompts.

## Decisions (from the ticket's grilling)

| Question | Decision | Why |
|---|---|---|
| Scope | Text attachments only | Solves the log problem. Images need a second input path whose Pi support is unknown. |
| Caps | 64 KB per file (first 8 KB, last 56 KB), 3 files and 128 KB per message, nothing over 5 MB downloaded | The start of a log shows its format, the end usually shows the fault. |
| Configurable | Yes, `chat.attachments` in `site.yaml` | Same place as `max_sessions` and `approval_ttl_seconds`; a change is a reviewed pull request. |
| Issue mirror | Description and permalink only | The issue is public and permanent; redaction is pattern-based and can miss a secret. |
| Redaction | Before the model sees it | A typed message is chosen; a pasted log is not reviewed, and the transcript and audit records keep what the model sees. |
| Rollout | 0.20.0, ansible pin PR, manual `files:read` reinstall | Without the scope the typed text still gets through, so order is not critical. |

## Pros and cons (the ticket's last question)

**Pros.** Long logs reach the agent without being pasted in pieces. The silent drop is fixed. The incident record notes that evidence was shared, with a link to it.

**Cons and what limits them.**

- Prompt injection through log lines: the block is labelled as data in both the prompt and the system prompt, only listed responders' files are read, and the tool policy and approvals still decide what the agent may do.
- Secrets in logs: redaction before the model; nothing copied to the issue. Pattern redaction can miss, which is why the issue never gets the content.
- Size and cost: bounded by the caps, which are configurable.
- Operations: one new Slack scope and a reinstall of the app.

## Known limits

- A secret that straddles the cut of a single line longer than 8,000 characters is not caught.
- Images, archives and other binaries are not read (the sender is told).
- A file larger than `max_download_mb` is not downloaded; the sender is asked for an excerpt.
- Slack Connect or externally owned files may refuse the download; the sender is told "Slack would not give the file to the app".

## Deviations from the plan

- Without `files:read`, Slack answers a download with an HTML sign-in page rather than a `missing_scope` error. The gateway detects that and says "does the app have the files:read scope?" instead of naming `missing_scope`.
- The plan listed `chat.py` fake updates; the fakes live in `gateway/tests/fakes.py`.
- Found while testing: keeping a private key whole across a piece boundary has to cut before its `BEGIN` line, not append past the scan limit, which would have lost the overflow unredacted. Covered by a test.

## Verification

Gateway: 190 tests, ruff, pylint (exit 0). Plugin: 798 tests, ruff, pylint (exit 0), `validate_site.py` on the template, identifier check clean. Not exercised against a live Slack workspace: that needs the `files:read` reinstall and the ansible deploy.

## Rollout

1. Merge the claude-plugins PR.
2. Add `files:read` to the Slack app (OAuth & Permissions) and reinstall to the workspace. Tokens do not change.
3. Merge the ansible PR that pins `sre_gateway_plugin_version`, and run the playbook on the gateway host.
4. Try it: paste a long log in the hub with an @mention and check the agent quotes it.
