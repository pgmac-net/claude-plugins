# homelabia#289: the SRE harness agent posts graphs to Slack (sre-harness 0.27.0)

Ticket: pgmac-net/homelabia#289 · PRs: pgmac-net/claude-plugins#71, pgmac-net/sre-catalog#111, pgmac-net/ansible#398 · Design: ADR 0027

## Problem

During an incident the chat gateway's agent was asked for graphs (sar, Grafana, nagiosgraph). It could draw them, with an approval for each render, but nothing it made reached Slack except as text. The workspace is on Slack's free plan.

## What was built

- **`scripts/share.py`**: hands an image to the gateway. It is allow-listed in the chat, triage and question policies.
  - The file must be a PNG, JPEG, GIF or WebP by its magic bytes. SVG is refused.
  - It must be no bigger than `chat.images.max_mb` (default 5 MB).
  - It must not be in a chat-policy deny path or the harness state, checked again after following links.
  - The script spools the image in the harness state.
- **Gateway**: recognises the `share.py` call and loads the image from the spool, not from what the script printed. The image must still hash to its name. It uploads with `files_upload_v2` (`getUploadURLExternal` + `completeUploadExternal`), the only upload method on the free plan, which needs the `files:write` scope. The image goes only to the conversation whose agent ran the call. If Slack refuses the upload, the channel is told why.
- **Record**: copies are kept only after the upload succeeds.
  - For an incident: one copy in the harness state and one in `<evidence_dir>/<id>/`. `audit.py commit` commits them beside the log as `<id>.images/`, and gc removes them.
  - The tracking issue gets one line per image (caption, size, digest, link), never the image.
  - For a question: a copy beside its log, pruned with it after 30 days.
- **`scripts/chart.py`**: draws PNGs read-only. It is allow-listed.
  - `prom`: a PromQL range query against the site's `slo` backend, through a new shared `slo.api_get`. Capped at 7 days and 20 series.
  - `nagios`: the nagiosgraph PNG through the status page's HMAC-signed graph proxy. The key comes from `~/.config/sre-harness/nagiosgraph-key`, a file the agent cannot read.
  - Output can only be a `.png` under the evidence or temp directory.
- **Schema**: `chat.images.max_mb`, and `monitoring[].graph_proxy {url, key_file}` (nagios entries only).
- **Slack manifest**: adds the `files:write` scope.
- **Agent prompt**: tells the agent to draw, look at the image with Read, then share it.

## Decisions (grilling)

| Question | Decision |
|---|---|
| How does the agent hand an image over? | An allow-listed harness script that the gateway recognises. Rejected: an SDK MCP tool (Claude Code only, Pi would need its own) and scanning replies for paths. |
| What may be shared without approval? | Real images by content, from any path except denied ones. |
| Which sessions? | Incidents, unattended triage and questions, each into its own place. |
| Durable record? | A description on the issue. The image itself goes in the evidence directory and in the audit-log commit. |
| Is rendering in scope? | Yes: `chart.py`. |
| Render sources? | Prometheus, plus nagiosgraph. |
| Nagios credential? | The proxy signing key in an owner-only file, following `slo.py`'s token-file pattern. |
| Render and share as one step or two? | Two separate calls. The agent looks at the image before anyone else does. |
| Which repos? | claude-plugins, sre-catalog and ansible. |

## Deviations from the plan

- The gateway, not `share.py`, decides which incident an image joins and makes the copies. The plan had the script do it. The gateway knows each session's incident for certain, so the agent cannot name another incident.
- Image helpers live in a new `scripts/image_files.py`, which breaks an import cycle: audit → share → chat_policy → audit.
- The image flow lives in a new `gateway/src/sre_gateway/images.py`. Keeping it in `gateway.py` would have pushed that file past pylint's 1000-line limit.
- **Security fix from an automated review:** under the gateway, `--site` may now name only the gateway's own site (`sre_common.resolve_site`). Before this, an allow-listed `chart.py`, `share.py` or `slo.py` given another site could send any environment variable, as the token, to any https host. `slo.py` had this exposure before this work too.
- `chart.py` and `share.py` are left out of srectl. `share.py` only does anything under the gateway.

## Verification

- claude-plugins:
  - ruff check and format: clean.
  - pylint (exit code) for `scripts tests` and for gateway `src tests`: 0.
  - pytest: plugin 1009 passed, 13 skipped; gateway 250 passed.
  - Template site valid, identifiers clean. CI passed.
- Live: `chart.py prom 'sum by (job) (up)' --since 3h` against the real site drew a correct 9-series chart in the site's time zone. `share.py` spooled it, and refused `site.yaml`. A `~/.claude` output path was refused.
- sre-catalog: the site passes `validate_site.py` against 0.27.0 and fails against 0.26.0, as expected. CI passed.
- ansible: `--syntax-check` passes. Not run against macro.
- Not run against Slack.

## Rollout (manual)

1. Reinstall the Slack app with `files:write`.
2. Merge claude-plugins#71. Pin `sre_gateway_plugin_version` (ansible `vars/sre_gateway.yml`) to the merge SHA.
3. Merge ansible#398 and run `sre-gateway.yml`. This writes the key file and restarts the gateway on 0.27.0.
4. Move sre-catalog#111's `SRE_HARNESS_REF` to the same merge SHA, then merge it. The gateway refuses a site with `graph_proxy` until it runs 0.27.0.
5. Run a drill: draw, Read, share in a thread. Then check the channel, the issue line, the evidence copy, and the audit branch after close-out.
